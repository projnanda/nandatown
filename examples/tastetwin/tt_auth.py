"""Sign a headless script into the Taste Twin MCP server (OAuth 2.1 + PKCE).

    python tt_auth.py start            registers a client once, prints a sign-in URL
    python tt_auth.py finish '<url>'   exchanges the code in the redirect URL for tokens
    python tt_auth.py token            prints a fresh access token (refreshes if needed)

Open the printed URL in any browser, sign in and approve. The browser then
lands on http://localhost:6585/oauth/callback?code=... which will not load:
copy that whole address and pass it to `finish`. Nothing listens on the port.

Tokens are stored in $TASTETWIN_STATE_DIR (default ~/.tastetwin-town) with
mode 0600. Never commit that directory.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

RESOURCE = os.environ.get("TASTETWIN_URL", "https://cafe-taste-twin.lovable.app/mcp")
ISSUER = os.environ.get("TASTETWIN_ISSUER",
                        "https://oxwryswjpgxtuyqjfrgs.supabase.co/auth/v1")
REDIRECT = "http://localhost:6585/oauth/callback"
SCOPE = "openid email offline_access"
USER_AGENT = "nandatown-tastetwin-example/0.1"


def state_path() -> str:
    base = os.environ.get("TASTETWIN_STATE_DIR", os.path.expanduser("~/.tastetwin-town"))
    return os.path.join(base, "state.json")


def _post(url: str, data: dict, form: bool = True) -> dict:
    body = urllib.parse.urlencode(data).encode() if form else json.dumps(data).encode()
    headers = {
        "content-type": "application/x-www-form-urlencoded" if form else "application/json",
        "accept": "application/json",
        "user-agent": USER_AGENT,
    }
    req = urllib.request.Request(url, body, headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"{url} -> HTTP {exc.code}: {exc.read().decode()[:300]}")


def _load() -> dict:
    path = state_path()
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def _save(state: dict) -> None:
    path = state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
    os.chmod(path, 0o600)


def start() -> str:
    state = _load()
    if "client_id" not in state:
        reg = _post(f"{ISSUER}/oauth/clients/register", {
            "client_name": "Nanda Town (HW8 test harness)",
            "redirect_uris": [REDIRECT],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }, form=False)
        state["client_id"] = reg["client_id"]
    state["verifier"] = secrets.token_urlsafe(64)
    state["state"] = secrets.token_urlsafe(16)
    digest = hashlib.sha256(state["verifier"].encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    _save(state)
    query = urllib.parse.urlencode({
        "response_type": "code", "client_id": state["client_id"],
        "redirect_uri": REDIRECT, "code_challenge": challenge,
        "code_challenge_method": "S256", "state": state["state"],
        "scope": SCOPE, "prompt": "consent", "resource": RESOURCE,
    })
    return f"{ISSUER}/oauth/authorize?{query}"


def _store_tokens(state: dict, tok: dict) -> None:
    state["access_token"] = tok["access_token"]
    state["refresh_token"] = tok.get("refresh_token", state.get("refresh_token"))
    state["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    _save(state)


def finish(redirected_url: str) -> None:
    state = _load()
    query = urllib.parse.parse_qs(urllib.parse.urlparse(redirected_url).query)
    if query.get("state", [None])[0] != state.get("state"):
        raise SystemExit("state mismatch: run `start` again and use the newest link")
    if "code" not in query:
        raise SystemExit("the URL has no code; copy the full address after approving")
    tok = _post(f"{ISSUER}/oauth/token", {
        "grant_type": "authorization_code", "code": query["code"][0],
        "redirect_uri": REDIRECT, "client_id": state["client_id"],
        "code_verifier": state["verifier"], "resource": RESOURCE,
    })
    _store_tokens(state, tok)


def token() -> str:
    state = _load()
    if "refresh_token" not in state:
        raise SystemExit("not signed in: run `tt_auth.py start`, then `finish`")
    if time.time() > state.get("expires_at", 0):
        tok = _post(f"{ISSUER}/oauth/token", {
            "grant_type": "refresh_token", "refresh_token": state["refresh_token"],
            "client_id": state["client_id"], "resource": RESOURCE,
        })
        _store_tokens(state, tok)
    return state["access_token"]


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else ""
    if cmd == "start":
        print(start())
    elif cmd == "finish" and len(argv) > 2:
        finish(argv[2])
        print(f"signed in; tokens saved to {state_path()}")
    elif cmd == "token":
        print(token())
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
