"""Optional real Chromium extraction of both fixtures; no model or external site."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
import os
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from nandatown.web_evidence import PlaywrightBrowser, RunConfig


@pytest.mark.skipif(os.environ.get("NANDATOWN_RUN_BROWSER_TESTS") != "1",
                    reason="Explicit local-browser opt-in required")
@pytest.mark.parametrize("variant", ["clean", "malicious"])
def test_atlas_browser_extracts_facts_and_keeps_oracle_unserved(variant):
    pytest.importorskip("playwright.sync_api")
    root = Path(__file__).parent / "fixtures/web_evidence/atlas_relay/site"
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(SimpleHTTPRequestHandler, directory=str(root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    browser = PlaywrightBrowser(RunConfig(url=f"{origin}/{variant}/index.html", question="Atlas?"))
    try:
        browser.start()
        page = browser.open_page(f"{origin}/{variant}/index.html")
        assert not page.snapshot_truncated
        assert "48,000" in page.visible_text and "72" in page.visible_text
        assert ("Agent 1 completion memo" in page.visible_text) == (variant == "malicious")
        with pytest.raises(HTTPError) as exc:
            urlopen(f"{origin}/oracle.json", timeout=2)
        assert exc.value.code == 404
    finally:
        browser.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
