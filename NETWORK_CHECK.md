# Network egress check — Naver endpoints

**Date:** 2026-09-01 (01:45 UTC)
**Container:** fresh Claude Code remote session on branch `claude/everland-review-collector-baii8v`

## What was tested

From the repo root, in a fresh container:

```bash
curl -sS -o /dev/null -w "%{http_code}" -m 20 https://pcmap.place.naver.com
curl -sS -o /dev/null -w "%{http_code}" -m 20 https://map.naver.com
```

## Result

Both requests failed identically:

| Host | http_code | curl error |
|---|---|---|
| `pcmap.place.naver.com` | `000` | `curl: (56) CONNECT tunnel failed, response 403` |
| `map.naver.com` | `000` | `curl: (56) CONNECT tunnel failed, response 403` |

The environment routes all outbound HTTPS through a pre-configured agent proxy.
Its status endpoint (`$HTTPS_PROXY/__agentproxy/status`) recorded both attempts as:

```
kind: connect_rejected
detail: gateway answered 403 to CONNECT (policy denial or upstream failure)
host: pcmap.place.naver.com:443  /  map.naver.com:443
```

## Conclusion

Egress to `naver.com` is **still blocked** from a fresh container: the proxy
gateway denies the CONNECT tunnel itself (policy denial), so no TLS connection
to any `*.naver.com` host can be established. This blocks both plain HTTP
fetches and the Playwright-driven collector equally.

No target discovery (Stage 2) or real collection run (Stage 3) was attempted;
neither is possible until the environment's network policy allows
`map.naver.com`, `pcmap.place.naver.com`, and `pcmap-api.place.naver.com`
(and ideally `naver.me` for short-link resolution).
