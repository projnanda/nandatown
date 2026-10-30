"""Local Northstar site lifecycle for the zero-argument demo command."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from urllib.request import urlopen


DEMO_QUESTION = "When did the Aurora Reasoning Engine project start?"


class DemoSiteUnavailable(RuntimeError):
    """The controlled local demo site could not be found or started."""


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, _format: str, *_args: object) -> None:
        pass


class _DemoServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


def find_demo_site() -> Path:
    candidates = (
        Path.cwd() / "examples" / "web_evidence_site",
        Path(__file__).resolve().parents[2] / "examples" / "web_evidence_site",
    )
    for candidate in candidates:
        if all((candidate / name).is_file() for name in (
                "index.html", "projects.html", "careers.html", "styles.css")):
            return candidate.resolve()
    raise DemoSiteUnavailable(
        "Northstar demo site not found; run this command from the NandaTown "
        "repository, or provide both --url and --question")


def _is_northstar_site(url: str) -> bool:
    try:
        with urlopen(url, timeout=2) as response:  # noqa: S310 - loopback only
            content = response.read(100_000).decode("utf-8", errors="replace")
        return "Northstar AI Labs" in content and "projects.html" in content
    except OSError:
        return False


@contextmanager
def serve_northstar_site(port: int = 8765) -> Iterator[str]:
    """Serve the clean fixture for one run, or reuse the same site if running."""
    root = find_demo_site()
    requested_url = f"http://127.0.0.1:{port}/index.html"
    handler = partial(_QuietHandler, directory=str(root))
    try:
        server = _DemoServer(("127.0.0.1", port), handler)
    except OSError as exc:
        if _is_northstar_site(requested_url):
            yield requested_url
            return
        raise DemoSiteUnavailable(
            f"port {port} is busy and is not serving the Northstar demo site") \
            from exc

    url = f"http://127.0.0.1:{server.server_port}/index.html"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield url
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
