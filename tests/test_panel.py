"""The panel is one big HTML page with inline JS: catch the mistakes no other test sees (0.38.x audit).

* every element the script looks up by a literal id exists in the page;
* every /api path the script calls is served by a route of the app.
"""
import re

from app.web import template


def _page() -> str:
    return template("panel.html")


def test_every_looked_up_id_exists():
    html = _page()
    ids = set(re.findall(r'id="([^"]+)"', html))
    used = set(re.findall(r"\$\('([^']+)'\)", html))
    assert used, "the panel script should look elements up with $('id')"
    missing = sorted(i for i in used if i not in ids)
    assert missing == [], f"$('…') without an element: {missing}"


def test_every_api_path_called_by_the_panel_has_a_route(client):
    html = _page()
    called = set(re.findall(r"(?:post|fetch)\(\s*'(/api/[^']*)'", html))
    assert called
    # included routers are not flat in app.routes any more: the OpenAPI schema lists every path
    routes = [getattr(r, "path", "") for r in client.app.routes] + list(client.app.openapi()["paths"])

    def served(path: str) -> bool:
        if path in routes:
            return True
        # '/api/stats/history/' + id → a route with a path parameter
        return path.endswith("/") and any(r.startswith(path) and "{" in r for r in routes)

    missing = sorted(p for p in called if not served(p))
    assert missing == [], f"API paths the panel calls but nothing serves: {missing}"
