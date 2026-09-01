# Real-data collection report (2026-09-01)

Status: **stabilized**. Three consecutive successful runs; the third collected
60 genuine Naver visitor reviews across all three configured venues with
consistent per-venue yield and zero cross-source duplicates.

## Verified targets (discovered from Naver Map search, addresses confirmed)

| Venue | place_id | Address | Category |
|---|---|---|---|
| 매직타임레스토랑 | 1527871697 | 경기도 용인시 처인구 포곡읍 에버랜드로 199 | 음식점 > 양식 |
| 알파인레스토랑 | 1678026825 | 경기도 용인시 처인구 포곡읍 에버랜드로 199 | 음식점 > 한식 |
| KFC 용인에버랜드 | 37747059 | 경기도 용인시 처인구 포곡읍 에버랜드로 199 | 양식 > 햄버거 |

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

## Field availability on real data (run 3, n=60)

| Field | Coverage | Note |
|---|---|---|
| review_id, reviewer_name, reviewer_profile_id, reviewer_profile_url | 100% | profile URL is the reviewer's public my-place page |
| visit_date, visit_count, helpful_count, verified_visit | 100% | dates resolved to YYYY-MM-DD; verification from 영수증/카드/예약 signals |
| keywords | 98% | Naver's voted keywords (e.g. "음식이 맛있어요") |
| review_text, review_image_count/urls | ~48% | present on full records; partial records have none |
| review_date | 50% | `created` only ships with full records |
| rating | 35% | Naver does not publish a star rating on every visitor review |
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

## Standing limitations

- GraphQL pagination is captcha-blocked from datacenter IPs; ~20 reviews per
  venue per run there. A residential-network run lifts this.
- The Claude Code sandbox itself still has no egress to naver.com; collection
  must run via the Actions workflow or a local machine.
- `rating`, `review_text` and `review_date` are structurally incomplete on
  partial records -- downstream analysis must treat them as nullable.
- Automated collection of Naver content is subject to Naver's terms of
  service; keep delays conservative and volumes modest.
