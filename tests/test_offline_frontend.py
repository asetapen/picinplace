"""The frame runs as a no-internet AP, so the web UI must not depend on any
external CDN. The React/Babel libraries have to be served locally."""
import asyncio
import re
from pathlib import Path

import server

VENDORED_LIBS = (
    "react.production.min.js",
    "react-dom.production.min.js",
    "babel.min.js",
)


def _frontend_html() -> str:
    resp = asyncio.run(server.serve_frontend())
    return resp.body.decode()


def test_frontend_loads_no_external_resources():
    """No script/link should point at an external host (works offline)."""
    html = _frontend_html()
    assert "unpkg.com" not in html
    assert not re.search(r'(src|href)\s*=\s*["\']https?://', html), \
        "frontend references an external URL; it will fail on the no-internet AP"


def test_frontend_references_local_vendored_libs():
    html = _frontend_html()
    for name in VENDORED_LIBS:
        assert f"/static/{name}" in html


def test_vendored_libs_exist_on_disk():
    static_dir = Path(server.__file__).parent / "static"
    for name in VENDORED_LIBS:
        f = static_dir / name
        assert f.exists(), f"missing vendored lib: {f}"
        assert f.stat().st_size > 1000, f"vendored lib looks truncated: {f}"
