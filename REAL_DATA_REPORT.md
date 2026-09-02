# Real-data collection report (2026-09-01)

Status: **complete**. The final crawl (run 33478435880) collected **715
genuine Naver visitor reviews across 29 of 30 discovered Everland venues**,
de-duplicating 437 repeats. One venue (온더보더 캐리비안베이점) is genuinely
review-thin on Naver rather than failing to collect.

| | |
|---|---|
| Venues discovered / with data | 30 / 29 |
| Reviews collected | 715 |
| Unique reviewers | 642 (51 with more than one review) |
| Visit-date span | 2019-04-01 - 2026-09-01 |
| Duplicates removed | 437 |

Integrity checks on the final dataset: all 715 `review_id`s unique with no
blanks, every `review_date`/`visit_date` a valid ISO date, all ratings within
0.5-5.0, list columns parsing back to lists with Korean intact.

## Verified targets

30 venues discovered from Naver Map search, each accepted only because the
address Naver returned places it on 에버랜드로 (Everland's street). The full
list with place ids lives in `config/restaurants.yaml`; `logs/discovery.log`
records how each was found.

## How the data is actually obtained (findings from real runs)

1. `pcmap.place.naver.com/<category>/<id>/review/visitor` server-renders the
   first review batch into an inlined, *normalized* Apollo cache: author
   records sit behind `{"__ref": ...}` pointers and a review's keywords /
   visit metadata live in sibling entities sharing its `reviewId`. The
   collector resolves the refs and stitches the fragments
   (`resolve_apollo_refs` / `extract_apollo_reviews`), yielding ~20 distinct
   reviews per venue per load: ~10 full (with body text) + ~10 partial
   (identity, visit data, keywords, reactions -- no body).
2. Follow-up pagination goes through POST `pcmap-api.place.naver.com/graphql`.
   **From datacenter IPs (GitHub Actions) Naver answers that POST with an
   HTTP 405 captcha page**, so 더보기 pagination cannot proceed there. The
   page itself is served normally; only the API calls are challenged.
3. Consequently, per run from Actions the yield is the SSR batch (~20/venue).
   Repeated runs over time accumulate newer reviews (dedup by review id makes
   re-runs safe). Deep pagination (hundreds per venue) requires running the
   same collector from a residential/normal network, where the page's own
   GraphQL calls are expected to pass and interception resumes automatically.

## Field availability on real data (run 33478435880, n=715)

| Field | Coverage | Note |
|---|---|---|
| review_id, reviewer_name, reviewer_profile_id, reviewer_profile_url | 100% | profile URL is the reviewer's public my-place page |
| visit_date, visit_count, helpful_count, verified_visit | 100% | dates resolved to YYYY-MM-DD; verification from 영수증/카드/예약 signals |
| keywords | 94% | Naver's voted keywords (e.g. "음식이 맛있어요") |
| review_text | 56% | present on full records; partial records have none |
| review_image_count/urls | 45% | |
| review_date | 59% | `created` only ships with full records |
| rating | 32% | Naver does not publish a star rating on every visitor review |
| menus | 0% | not present in any observed payload; alias list ready if it appears |

## Operating the pipeline

- Trigger a collection: push to this branch with `[collect]` in the commit
  message, or run the `collect-reviews` workflow manually once this file tree
  reaches the default branch. Inputs: queries, max_targets, only, max_reviews,
  delay, skip_discovery.
- Outputs are committed back to the branch: `data/raw/*.json` (verbatim
  payloads + archived responses incl. the full Apollo state), `data/normalized/
  reviews_*.csv` + `summary_*.json`, and `logs/` (discovery + collect logs,
  debug page snapshots when a page yields no API data).
- Offline verification: `python tests/test_pipeline.py` (no network needed).
- Re-normalize archived raw data after schema changes:
  `python scripts/collect_reviews.py --replay-raw data/raw`.

## Known issues found and fixed

- **The run budget skipped venues (run 33474717994).** Four venues were
  never attempted because the 2700s budget expired first -- the skip was
  graceful and recorded, but the budget was simply too tight at the measured
  ~112s per venue. Raised to 4200s, after which 29 of 30 venues collected.
- **Cafes returned nothing (run 33468327404).** All 10 venues configured with
  the `cafe` category yielded zero: `pcmap.place.naver.com/cafe/<id>/review/visitor`
  serves a completely empty document, while `/restaurant/<id>/...` works for
  those same places. The collector now treats the category segment as a
  fallback chain and retries under `/restaurant/`, abandoning a base path that
  yields nothing after a single request. Discovery no longer emits `cafe`.
- **A finished crawl was lost to a rejected push (run 33468274642).** It
  collected all 30 venues, then failed its `git push` as non-fast-forward
  because the branch moved while it ran. The workflow now rebases and retries.
- **A cancelled crawl would have lost everything (run 33464338587).** The
  pipeline built every result before writing any file; it now persists each
  venue as it completes and bounds the run with a wall-clock budget.

## Standing limitations

- GraphQL pagination is sometimes challenged from datacenter IPs (a CAPTCHA
  page on the POST), which caps a run at the server-rendered batch of ~25-40
  reviews per venue. It is not always challenged -- the 신리천 crawl paged
  freely -- so yield per run varies with runner IP and timing rather than
  with anything the collector does.
- The Claude Code sandbox itself still has no egress to naver.com; collection
  must run via the Actions workflow or a local machine.
- `rating`, `review_text` and `review_date` are structurally incomplete on
  partial records -- downstream analysis must treat them as nullable.
- Automated collection of Naver content is subject to Naver's terms of
  service; keep delays conservative and volumes modest.

---

# 신리천 카페거리, 동탄 (2026-09-02)

Run 33593161153 collected **2,914 reviews across 21 of 22 venues**.

| | |
|---|---|
| Venues verified / with data | 22 / 21 |
| Reviews collected | 2,914 |
| Unique reviewers | 1,999 (378 with more than one review) |
| Visit-date span | 2019-02-25 - 2026-09-01 |
| Duplicates removed | 553 |
| Collection failures | 2 |

Targets were verified by proximity rather than address: a centre resolved
from Naver at run time (37.184793, 127.103619), venues within 700m, median
distance 95m, 20 of 22 on 동탄대로14길. See `config/targets/sinlicheon.yaml`.

## The GraphQL pager worked here

This is the significant finding. **2,637 of the 2,914 reviews came from
intercepted GraphQL responses** (326 archived), with only 277 from the
server-rendered batch. No CAPTCHA appeared at any point; the single API
failure was one HTTP 429.

So the ~25-40 reviews per venue that bounded every Everland run was an
artifact of those runs being challenged, **not a ceiling of the method**.
The interception path built for that case works whenever Naver does not
challenge the request, and engages by itself. Whether a run is challenged
appears to track runner IP and timing rather than anything the collector
does, so per-run yield is not fully predictable.

Practical consequence: **the Everland dataset is worth re-collecting.** Its
715 reviews came entirely from server-rendered batches; an unchallenged run
should return substantially more.

## Field availability (n=2,914)

| Field | Coverage | Note |
|---|---|---|
| platform, place_*, visit_count, collected_at, raw_ref | 100% | |
| review_id, reviewer_*, visit_date, helpful_count, verified_visit | 98.5% | 43 records carry no platform id |
| review_date | 96% | |
| keywords | 94% | |
| review_text | 93% | far higher than Everland's 56% |
| review_image_count/urls | 64% | |
| rating | 25% | still the sparsest field |
| menus | 0% | absent from every observed payload |

Ordinary neighbourhood venues publish much more body text than Everland's
in-park outlets (93% vs 56%), but `rating` stays sparse on both, so
downstream code must keep treating it as optional.

## Caveats on this dataset

- **Ten venues stopped at exactly 200**, the configured per-venue cap rather
  than exhaustion: 포해피니스, 카시아 본관, 오브느, 명태어장, 써먼하우스,
  세븐야드, 월남국수, 휘드헨느, 91brick, 스타벅스. More reviews are available
  for them at a higher `--max-reviews`.
- **43 records (1.5%) have no platform `review_id`** and are de-duplicated by
  content hash instead. They are otherwise complete.
- **디저트39 동탄신리천점 returned nothing**, with one HTTP 429 recorded, so
  this is rate limiting rather than a diagnosed absence of reviews.
- Validated: all present `review_id`s unique, every date valid ISO, ratings
  within 1.0-5.0, Korean intact through JSON and CSV.
