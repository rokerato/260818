# Everland Review Collection Pipeline

Step 1 of a review-analysis service for restaurants and cafes inside Everland:
a reproducible pipeline that collects **real** public review data, archives the
untouched platform payloads, and emits a normalized dataset.

Scope is deliberately narrow. There is **no** fake-review detection, trust
scoring, sentiment analysis, recommendation logic or frontend here — only
collection, normalization, storage and validation.

---

## 1. Platform investigation: how Naver actually serves reviews

Naver Map was the first candidate, and the inspection changed the design.

| Question | Finding |
|---|---|
| Present in the initial HTML? | **No.** `map.naver.com` returns a JavaScript application shell. |
| Dynamically rendered? | **Yes.** The place detail view is a separate document served from `pcmap.place.naver.com/<category>/<id>/review/visitor` (`m.place.naver.com` on mobile), embedded as an iframe by the map shell. |
| Loaded through network requests? | **Yes.** That document inlines the first batch in an Apollo cache; every later batch is a client-side **POST to `pcmap-api.place.naver.com/graphql`**. |
| Paginated or infinite-scroll? | Neither, strictly. It is a **`더보기` ("load more") pager** — roughly ten reviews per press. No page numbers, no scroll-triggered fetch. |
| Client-side API? | Yes — GraphQL, with a rotating query document and Naver-specific headers (notably a `Referer` that must match the place page). |

Two consequences drove the implementation:

1. **A plain HTTP fetch of the map URL yields nothing.** Any collector has to
   reach the `pcmap` document, not `map.naver.com`.
2. **Hand-writing the GraphQL request is the fragile option.** The operation
   document and persisted-query hashes rotate, and the request must carry
   headers the page sets for itself. Reimplementing that means re-reverse-engineering
   it every time Naver ships a change.

### Chosen method: drive the page, intercept its own API responses

The collector opens the real review page in Chromium, presses `더보기` until the
requested limit is reached, and **captures the GraphQL responses the page fetches
for itself**. It never constructs a GraphQL query.

That buys three things:

* **Resilience** — Naver rotating its query text or persisted-query hash does not
  break collection, because the page issues the query.
* **Fidelity** — what lands in `data/raw/` is the authentic upstream JSON, so a
  later schema change can be replayed offline instead of re-crawling (`--replay-raw`).
* **No selector dependence** on the primary path. Naver rotates hashed CSS class
  names constantly; response interception is immune to that.

A **DOM fallback** reads the rendered list if interception yields nothing. Its
selectors are configurable, and it is explicitly best-effort — it records only
text that is actually on the page.

Review nodes are located inside captured responses **structurally** (a dict
carrying an id plus two or more review-shaped keys), not by a hard-coded JSON
path, so an envelope rename does not break extraction.

---

## 2. Layout

```
config/restaurants.yaml        # targets + settings; the only file you edit to add a venue
everland_reviews/
  config.py                    # YAML -> PlaceTarget / Settings
  collectors/
    base.py                    # BaseCollector, PlaceTarget, RawReview, CollectionResult
    naver.py                   # Naver Place collector (graphql interception + DOM fallback)
  normalize/
    schema.py                  # NormalizedReview -- platform-agnostic
    naver_mapper.py            # raw Naver payload -> NormalizedReview
  storage.py                   # raw JSON archive, normalized CSV, summary JSON
  validation.py                # per-restaurant + overall summaries, integrity checks
  pipeline.py                  # collect -> archive -> normalize -> validate -> report
scripts/collect_reviews.py     # CLI
tests/
  fixture_server.py            # local stand-in for a Naver review page (synthetic data)
  test_pipeline.py             # end-to-end verification, no network
data/raw/  data/normalized/    # outputs
```

**The collector never imports the normalized schema.** It emits raw platform
payloads; a per-platform mapper translates them. Adding a platform means adding
a collector plus a mapper and registering them — the schema and every downstream
analysis step stay untouched.

---

## 3. Setup

```bash
pip install -r requirements.txt
playwright install chromium
```

## 4. Configure targets

`config/restaurants.yaml` ships **empty on purpose** — place ids must come from a
real lookup, so no URLs are guessed or invented here.

To add a venue: search it on <https://map.naver.com>, click the place, and take
the trailing number from `https://map.naver.com/p/entry/place/1234567890`.

```yaml
restaurants:
  - name: "Restaurant A"
    url: "https://map.naver.com/p/entry/place/1234567890"

  - name: "Cafe B"
    place_id: "1234567890"
    category: "cafe"        # 'restaurant' (default) or 'cafe'
    max_reviews: 300        # optional per-restaurant override
```

Accepted forms: `map.naver.com/p/entry/place/<id>`, `map.naver.com/v5/entry/place/<id>`,
`pcmap.place.naver.com/<category>/<id>/...`, `m.place.naver.com/<category>/<id>/...`,
or a bare numeric id. A `naver.me` short link is resolved by the browser at runtime.

## 5. Run

```bash
python scripts/collect_reviews.py --config config/restaurants.yaml
python scripts/collect_reviews.py --max-reviews 50 --only "카페"
python scripts/collect_reviews.py --replay-raw data/raw     # re-normalize, no network
```

Useful flags: `--delay` (seconds between interactions), `--csv-bom` (utf-8-sig for
Excel on Windows), `--headful`, `--browser-executable`, `--raw-dir`, `--normalized-dir`.

### Outputs

* `data/raw/naver_<slug>_<stamp>.json` — verbatim review payloads **and** the raw
  GraphQL responses, plus resolved place info and every recorded failure.
* `data/normalized/reviews_<stamp>.csv` — the normalized dataset (UTF-8).
* `data/normalized/summary_<stamp>.json` — per-restaurant counts, failures and
  per-field coverage.

## 6. Normalized schema

21 columns. **Every field is nullable, and an unavailable field is written empty
— never a placeholder, never inferred.**

`platform`, `place_name`, `place_id`, `place_url`, `review_id`, `reviewer_name`,
`reviewer_profile_id`, `reviewer_profile_url`, `rating`, `review_text`,
`review_date`, `visit_date`, `visit_count`, `review_image_count`,
`review_image_urls`, `helpful_count`, `keywords`, `menus`, `verified_visit`,
`collected_at`, `raw_ref`

* List columns (`review_image_urls`, `keywords`, `menus`) are JSON-encoded with
  `ensure_ascii=False`, so Korean survives and the value round-trips to a list.
* `raw_ref` (`<raw_file_stem>#<index>`) points each normalized row back at its raw payload.
* Dates normalize to `YYYY-MM-DD` when parseable (including Naver's `24.5.3.금`
  form); anything unrecognised is kept verbatim rather than dropped.

## 7. Collection behaviour

* Accepts a place URL **or** a place id; multiple targets per run.
* Presses `더보기` until the limit is hit, then stops after
  `max_empty_rounds` consecutive rounds that add nothing.
* De-duplicates on the platform review id, falling back to a content hash when
  no id is published; the count is reported.
* Conservative pacing between interactions (`request_delay_seconds`, default 1.5s).
* A failing target is logged and recorded — it never aborts the run. So is a bad
  page, an unreadable response, or a payload that fails to normalize.
* Configurable per-restaurant and global review caps.

## 8. Validation

Each run prints per-restaurant and overall summaries, a field-availability table
(what the platform actually published), and integrity warnings: duplicate ids that
survived de-duplication, missing ids, missing place ids, out-of-range ratings, and
a dataset with no text at all.

## 9. Tests

```bash
python tests/test_pipeline.py
```

Runs the full pipeline against a **local fixture server** that reproduces the shape
of a Naver review page — JS-rendered list, GraphQL-backed batches, a `더보기` pager,
a review re-served across a page boundary, and rows with fields deliberately absent.

The fixture content is obviously synthetic placeholder text. It verifies the
*machinery* (interception, pagination, de-duplication, null handling,
normalization, CSV/JSON output, failure isolation, offline replay). It is **not**
a sample of real Naver data.

---

## 10. Known limitations

* **Rating is frequently absent.** Naver does not publish a numeric star rating on
  every visitor review, so `rating` is null more often than on other platforms.
  Downstream code must not assume it is present.
* **Login-gated fields are out of reach.** Anything Naver shows only to a signed-in
  user is not collected. This pipeline reads public data only.
* **Selector rot affects the fallback path.** The `더보기` locator and DOM fallback
  use text and structure rather than hashed class names, but Naver layout changes
  can still require updating the configurable selectors in `collectors/naver.py`.
* **Rate limiting / bot detection.** Sustained collection may be throttled. Keep
  `request_delay_seconds` conservative and collect in modest batches.
* **Terms of service.** Automated collection of Naver content is subject to
  Naver's terms. Confirm the intended use is permitted before running at volume.

## 11. Not in this step

No sentiment analysis, fake-review classification, trust scoring, recommendation
logic or frontend. The output is the clean, reproducible input those later stages
will consume.
