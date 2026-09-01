"""A local stand-in for a Naver Place visitor-review page.

It reproduces the *shape* of the real thing -- a JS-rendered review list whose
batches arrive over a GraphQL POST behind a 더보기 pager -- so the collector's
interception, pagination, de-duplication and null handling can be exercised
without touching Naver.

The review content is obviously synthetic placeholder text. It is test scaffolding,
never a sample of real data.
"""

from __future__ import annotations

import contextlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PAGE_SIZE = 10

_PAGE_HTML = """<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><title>테스트 장소 방문자 리뷰</title></head>
<body>
  <h1 id="place-name">테스트 장소</h1>
  <ul id="_review_list"></ul>
  <a href="#" class="fvwqf" id="more">더보기</a>
  <script>
    let page = 0;
    async function loadPage() {
      page += 1;
      const res = await fetch('/graphql', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({
          operationName: 'getVisitorReviews',
          variables: {input: {businessId: '__PLACE_ID__', page: page, size: %d}}
        })
      });
      const data = await res.json();
      const items = data.data.visitorReviews.items;
      const list = document.getElementById('_review_list');
      for (const item of items) {
        const li = document.createElement('li');
        li.className = 'place_apply_pui';
        li.textContent = (item.author ? item.author.nickname : '') + ' ' + (item.body || '');
        list.appendChild(li);
      }
      if (!data.data.visitorReviews.hasNext) {
        document.getElementById('more').style.display = 'none';
      }
    }
    document.getElementById('more').addEventListener('click', function (e) {
      e.preventDefault();
      loadPage();
    });
    loadPage();
  </script>
</body></html>
""" % PAGE_SIZE


def make_review(index: int) -> dict:
    """Build one synthetic review, varying which fields are present."""
    review: dict = {
        "__typename": "VisitorReview",
        "id": f"fixture-review-{index:03d}",
        "author": {
            "id": f"user{index % 7:02d}",
            "nickname": f"테스트사용자{index % 7:02d}",
            "url": f"https://example.invalid/my/user{index % 7:02d}",
        },
        "created": f"2025-{(index % 12) + 1:02d}-{(index % 27) + 1:02d}T09:00:00",
        "visitCount": f"{(index % 3) + 1}번째 방문",
        "reactionStat": {"totalCount": index % 5},
        "originType": "RECEIPT" if index % 4 else "NONE",
    }
    # Every 5th review carries no text -- exercises the "with_text" counter.
    if index % 5:
        review["body"] = f"테스트 리뷰 본문 {index}. 음식이 맛있고 직원이 친절했습니다."
    # Every 3rd review carries no rating -- Naver does not always publish one.
    if index % 3:
        review["rating"] = round(3.0 + (index % 5) * 0.5, 1)
    if index % 2:
        review["visited"] = f"25.{(index % 12) + 1}.{(index % 27) + 1}.수"
    if index % 4 == 0:
        review["media"] = [
            {"thumbnail": f"https://example.invalid/img/{index}_{n}.jpg"}
            for n in range(1 + index % 3)
        ]
    if index % 6 == 0:
        review["votedKeywords"] = [
            {"displayName": "음식이 맛있어요"},
            {"displayName": "매장이 청결해요"},
        ]
    if index % 7 == 0:
        review["menus"] = [{"name": "테스트메뉴"}]
    return review


class _Handler(BaseHTTPRequestHandler):
    total_reviews = 24
    place_id = "9999999999"
    #: Re-serve this review id on a later page, so de-duplication is exercised.
    duplicate_on_page = 2

    def log_message(self, *args):  # noqa: D102 - silence per-request logging
        pass

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        # Only the configured place exists, so tests can exercise a bad target.
        if "/review/visitor" in self.path and f"/{self.place_id}/" in self.path:
            html = _PAGE_HTML.replace("__PLACE_ID__", self.place_id)
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
        else:
            self._send(
                404,
                "<html><body>존재하지 않는 장소입니다</body></html>".encode("utf-8"),
                "text/html; charset=utf-8",
            )

    def do_POST(self) -> None:  # noqa: N802
        if not self.path.startswith("/graphql"):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        length = int(self.headers.get("Content-Length", 0))
        try:
            request = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            request = {}
        page = int(
            request.get("variables", {}).get("input", {}).get("page", 1) or 1
        )
        start = (page - 1) * PAGE_SIZE
        indexes = list(range(start, min(start + PAGE_SIZE, self.total_reviews)))
        items = [make_review(i) for i in indexes]
        if page == self.duplicate_on_page and items:
            # The real API occasionally re-serves a row across page boundaries.
            items.append(make_review(0))
        body = json.dumps(
            {
                "data": {
                    "visitorReviews": {
                        "__typename": "VisitorReviews",
                        "total": self.total_reviews,
                        "hasNext": start + PAGE_SIZE < self.total_reviews,
                        "items": items,
                    }
                }
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self._send(200, body, "application/json; charset=utf-8")


@contextlib.contextmanager
def serve(total_reviews: int = 24, place_id: str = "9999999999"):
    """Run the fixture server on an ephemeral port; yields its base URL."""
    handler = type(
        "_BoundHandler",
        (_Handler,),
        {"total_reviews": total_reviews, "place_id": place_id},
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
