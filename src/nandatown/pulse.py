"""Town Pulse: operational history after publication.

A sandbox test at onboarding is one moment and cannot show next week.
Pulse checks each registered service on a schedule and keeps the full
history, so availability is a measured record, not a memory. Every
probe becomes one operational-history evidence record: one observer,
one subject, one time.
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

import httpx

from .records import EvidenceRecord
from .url_credentials import Labeller

OBSERVER = "town-pulse.v1"


def unprobeable(url: object) -> str | None:
    """Why this URL can never be probed at all, or None.

    Parsing a URL is not the same as being able to use it: httpx decodes
    a punycode hostname only when the host is read, and a malformed
    A-label raises there. Both failures are the operator's typo, not a
    service being down, and neither is an httpx.HTTPError.
    """
    if not isinstance(url, str):
        return "must be a string"
    try:
        parsed = httpx.URL(url)
        if not parsed.host:
            return "no host"
    except (httpx.InvalidURL, UnicodeError) as exc:
        return str(exc) or type(exc).__name__
    return None


def probe(url: str, timeout: float = 3.0) -> dict[str, Any]:
    started = time.time()

    def outcome(error: str) -> dict[str, Any]:
        return {"ok": False, "status": 0,
                "latency_ms": round((time.time() - started) * 1000, 1),
                "error": error}

    # A schedule outlives any one probe, so nothing a single target does
    # may end it: a target that cannot be probed is recorded as down,
    # like one that cannot be reached, and the others keep their history.
    problem = unprobeable(url)
    if problem is not None:
        return outcome("unprobeable URL")
    try:
        response = httpx.get(url, timeout=timeout)
        return {"ok": response.status_code < 500,
                "status": response.status_code,
                "latency_ms": round((time.time() - started) * 1000, 1)}
    except httpx.HTTPError as exc:
        return outcome(type(exc).__name__)


def _conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS probes ("
        " name TEXT NOT NULL, url TEXT NOT NULL, at REAL NOT NULL,"
        " ok INTEGER NOT NULL, status INTEGER NOT NULL,"
        " latency_ms REAL NOT NULL)")
    return conn


def run_pulse(targets: dict[str, str], count: int, interval: float,
              db_path: str, on_probe=None) -> None:
    """Probe each target as written and keep its history.

    Credentials in a target URL are sent with each probe but never
    stored: history records the URL with its credentials labelled, so two
    sets of credentials for one host stay separate endpoints.
    """
    label = Labeller()
    with _conn(db_path) as conn:
        for i in range(count):
            for name, url in targets.items():
                result = probe(url)
                conn.execute(
                    "INSERT INTO probes (name, url, at, ok, status,"
                    " latency_ms) VALUES (?,?,?,?,?,?)",
                    (name, label.label(url), time.time(), int(result["ok"]),
                     result["status"], result["latency_ms"]))
                conn.commit()
                if on_probe:
                    on_probe(name, result)
            if i < count - 1:
                time.sleep(interval)


def availability(db_path: str) -> dict[str, dict[str, Any]]:
    """Availability per target name, attributed to the endpoint probed.

    A name is an operator's label, not an identity, and can be re-pointed
    at another URL. Probes of different URLs are never blended: the
    headline figures describe the URL the name was probed at most
    recently, and each earlier URL keeps its own figures, in order of its
    last probe, under ``previous_endpoints``. URLs are compared exactly,
    and a URL a name returns to keeps all of its probes.

    Credentials are compared by label. History written before labelling
    existed stored them in the URL, and is labelled as it is read, so it
    joins the history recorded since instead of printing them.
    """
    label = Labeller()
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT name, url, at, ok, latency_ms FROM probes"
            " ORDER BY at, rowid").fetchall()
    series: dict[str, dict[str, dict[str, Any]]] = {}
    for name, stored, at, ok, latency in rows:
        url = label.label(stored)
        endpoints = series.setdefault(name, {})
        # Re-insert so each name's endpoints stay ordered by latest probe.
        entry = endpoints.pop(url, None) or {
            "url": url, "checks": 0, "up": 0, "first_at": at,
            "last_at": at, "last_ok": bool(ok), "latencies": []}
        endpoints[url] = entry
        entry["checks"] += 1
        entry["up"] += ok
        entry["last_at"] = at
        entry["last_ok"] = bool(ok)
        if ok:
            entry["latencies"].append(latency)
    out: dict[str, dict[str, Any]] = {}
    for name, endpoints in series.items():
        for entry in endpoints.values():
            entry["availability"] = round(100.0 * entry["up"]
                                          / entry["checks"], 1)
            lat = entry.pop("latencies")
            entry["median_latency_ms"] = (sorted(lat)[len(lat) // 2]
                                          if lat else None)
        *previous, current = endpoints.values()
        current["previous_endpoints"] = previous
        out[name] = current
    return out


def export_records(db_path: str) -> list[EvidenceRecord]:
    label = Labeller()
    with _conn(db_path) as conn:
        rows = conn.execute(
            "SELECT rowid, name, url, at, ok, status FROM probes"
            " ORDER BY at").fetchall()
    return [
        EvidenceRecord(
            record_id=f"pulse-{rowid}", observer=OBSERVER, subject=name,
            capability="liveness", test="http-probe",
            result="passed" if ok else "failed", at=at,
            evidence=[f"{label.label(url)} responded {status}" if ok
                      else f"{label.label(url)} unreachable or {status}"])
        for rowid, name, url, at, ok, status in rows
    ]


def render_pulse_report(db_path: str) -> str:
    stats = availability(db_path)
    if not stats:
        return "no pulse history yet\n"
    lines = ["Town Pulse operational history", "=" * 40]
    width = max(len(n) for n in stats)
    for name, s in sorted(stats.items()):
        state = "up" if s["last_ok"] else "DOWN"
        latency = (f", median {s['median_latency_ms']:.0f} ms"
                   if s["median_latency_ms"] is not None else "")
        lines.append(
            f"{name.ljust(width)}  {s['availability']:5.1f}% of"
            f" {s['checks']} checks, now {state}{latency}")
        if s["previous_endpoints"]:
            indent = " " * (width + 2)
            lines.append(f"{indent}current endpoint {s['url']}")
            for p in s["previous_endpoints"]:
                last = "up" if p["last_ok"] else "DOWN"
                lines.append(
                    f"{indent}earlier endpoint {p['url']}:"
                    f" {p['availability']:.1f}% of {p['checks']} checks,"
                    f" last {last}")
    lines.append("")
    lines.append("A one-time test at publish time would show none of"
                 " this. History is the evidence.")
    return "\n".join(lines) + "\n"
