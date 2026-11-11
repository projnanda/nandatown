"""Tiny standard-library HTTP helper shared by the two known-truth
participants. Nothing here imports nandatown."""

import json
import os
import time
import urllib.error
import urllib.request

TOWN = os.environ.get("TOWN_URL", "")
RUN = os.environ.get("RUN_ID", "")
session = None


def call(method, path, body=None, retry=True):
    url = f"{TOWN}/runs/{RUN}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    if session:
        req.add_header("X-Town-Session", session)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 204:
                return None
            return json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        if exc.code == 503 and retry:
            time.sleep(0.2)
            return call(method, path, body, retry=False)
        raise


def join(name, token):
    global session
    joined = call("POST", "/join", {"name": name, "token": token})
    session = joined["session"]
    return joined
