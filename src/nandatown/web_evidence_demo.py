"""Verified local Northstar site lifecycle for the demo command."""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from urllib.parse import urlsplit


DEMO_QUESTION = "When did the Aurora Reasoning Engine project start?"
_EXPECTED_HASHES = {
    "index.html": "6d23e3a9f8a26cf360ff8d2b982fcb73544db94443c5b29b60ae0aac11ee1507",
    "projects.html": "d268afb013b9287b41ce613993c4dee658d617182ba7192ebd4aa67bb86a361d",
    "careers.html": "64587341f39e9bb06115e192042181740233e66198cf43a68fca673a70c58c78",
    "styles.css": "19404704c4c1918df88e01f33b332483c71a6b5dffe4deca490d1c79ade6fffc",
}
_CONTENT_TYPES = {
    "index.html": "text/html; charset=utf-8",
    "projects.html": "text/html; charset=utf-8",
    "careers.html": "text/html; charset=utf-8",
    "styles.css": "text/css; charset=utf-8",
}


class DemoSiteUnavailable(RuntimeError):
    """The controlled local demo site could not be verified or started."""


def find_demo_site() -> Path:
    """Return only the repository-owned demo directory."""
    root = Path(__file__).resolve().parents[2]
    site = root / "examples" / "web_evidence_site"
    if not site.is_dir() or site.is_symlink():
        raise DemoSiteUnavailable(
            "verified Northstar demo site not found in the NandaTown repository")
    return site


def _load_demo_assets(site: Path) -> dict[str, bytes]:
    assets = {}
    for name, expected_hash in _EXPECTED_HASHES.items():
        path = site / name
        if not path.is_file() or path.is_symlink() or path.parent != site:
            raise DemoSiteUnavailable(f"invalid Northstar demo asset: {name}")
        content = path.read_bytes()
        if hashlib.sha256(content).hexdigest() != expected_hash:
            raise DemoSiteUnavailable(f"Northstar demo asset failed verification: {name}")
        assets[name] = content
    return assets


def _handler(assets: dict[str, bytes]):
    class VerifiedDemoHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            pass

        def do_HEAD(self) -> None:  # noqa: N802 - standard-library callback
            self._serve(include_body=False)

        def do_GET(self) -> None:  # noqa: N802 - standard-library callback
            self._serve(include_body=True)

        def _serve(self, *, include_body: bool) -> None:
            requested = urlsplit(self.path).path
            name = "index.html" if requested == "/" else requested.removeprefix("/")
            content = assets.get(name)
            if content is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", _CONTENT_TYPES[name])
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if include_body:
                self.wfile.write(content)

    return VerifiedDemoHandler


class _DemoServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


@contextmanager
def serve_northstar_site(port: int = 8765) -> Iterator[str]:
    """Serve verified in-memory assets; refuse an occupied port."""
    assets = _load_demo_assets(find_demo_site())
    try:
        server = _DemoServer(("127.0.0.1", port), _handler(assets))
    except OSError as exc:
        raise DemoSiteUnavailable(
            f"port {port} is busy; stop the other server and retry") from exc

    url = f"http://127.0.0.1:{server.server_port}/index.html"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
