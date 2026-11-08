#!/usr/bin/env python3
"""A stand-in model that answers like Ollama, at once, and takes the rule's plan at every point. Nothing it says is an agent's decision.

  python examples/flexgrid/stand_in_model.py 50399 &
  python examples/flexgrid/run_check.py --cases normal --model http://127.0.0.1:50399 --model-name stand-in

Two uses: a dry run of the agent days without a model, and a yardstick. Every answer is the rule's (a note excepted: it keeps what the note
names, which a rule can't read), so on a day with no note the gap between its day and the rules day is the agent framework's own, not a
model's: the agents' plans, the guard and their device calls. --cut NODE makes that node keep 20% of its
battery back with a reason, so a cut, its receipt and the owner's message show on a dry run too.

  python stand_in_model.py PORT [--ms 50] [--cut hh-039] [--log FILE]

What it answers:
  an offer           the planner's safe offer in full, with a reason. A node named by --cut keeps 20% of its battery back and cites a fact,
                     so a cut with a reason, its receipt and the owner's message are on the dry run too
  a commitment       yes, with a reason
  a plan (set_plan)  the letters code would open ("If you change nothing, code opens A, C ..."), with a reason
  nothing to open    no_action, with a reason
  a pre-cool         the playbook's depth on the target offered
  a note             keep_battery_full, keep_car_ready or keep_house_as_is from the note's words, else no_wish
  a report           two plain sentences with no numbers
  anything else      no_action with a reason if offered, else the first tool, with allowed values
"""
import argparse
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap = argparse.ArgumentParser()
ap.add_argument("port", type=int)
ap.add_argument("--ms", type=float, default=50.0)
ap.add_argument("--cut", default="", help="comma separated nodes that offer less than the rule's amount, with a reason")
ap.add_argument("--log", default="")
ap.add_argument("--name", default="stand-in")
a = ap.parse_args()
CUT = {x for x in a.cut.split(",") if x}
lock = threading.Lock()
count: dict = {"chat": 0}
DEFAULT = re.compile(r"If you change nothing, code opens (.+?) and [\d.,]+ kW stay uncovered")
PRECOOL = re.compile(r"(\S+) may go (-?[\d.]+) C")


def fill(schema: dict, hints: dict) -> dict:
    """Every required field, with an allowed value."""
    out = {}
    props = schema.get("properties") or {}
    for k in schema.get("required") or list(props):
        v = props.get(k) or {}
        if k in hints:
            out[k] = hints[k]
        elif v.get("enum"):
            out[k] = v["enum"][0]
        elif v.get("type") == "number" or v.get("type") == "integer":
            out[k] = v.get("maximum", 1.0) if "share" in k else v.get("minimum", 0) if v.get("minimum", 0) > 0 else 1.0
        elif v.get("type") == "boolean":
            out[k] = True
        elif v.get("type") == "array":
            out[k] = []
        else:
            out[k] = "stand-in"
    return out


def node_of(text: str) -> str:
    m = re.search(r'"(?:home|building|name)": "((?:hh|bld)-[\w-]+)"', text)
    return m.group(1) if m else ""


def structured(schema: dict, text: str) -> tuple[str, dict]:
    props = schema.get("properties") or {}
    node = node_of(text)
    if "take_part" in props:
        cut = node in CUT
        ids = [x for x in (props.get("cut_fact") or {}).get("enum") or [] if x != "none"]
        why = ("I keep a fifth of the battery back for the morning, as the facts allow." if cut and ids else
               "I offer the planner's safe offer in full: nothing tonight calls for less.")
        hints = {"take_part": True, "battery_share": 0.8 if cut and ids else 1.0, "cooling_share": 1.0, "lighting_share": 1.0, "pause_ev": True,
                 "devices_off": [], "cut_fact": ids[0] if cut and ids else "none", "reason_for_owner": why, "reason_for_manager": why, "reason_for_log": why}
        return ("offer-cut" if cut and ids else "offer"), fill(schema, hints)
    if "accept" in props:
        why = "Nothing changed since the offer, so I keep it."
        return "commitment", fill(schema, {"accept": True, "decline_fact": "none", "reason_for_owner": why, "reason_for_manager": why, "reason_for_log": why})
    if "explanation" in props:
        return "explain", {"explanation": "The call covers the grid's need tonight."}
    return "other", fill(schema, {})


def with_tools(tools: list, text: str) -> tuple[str, dict]:
    by = {t["function"]["name"]: t["function"] for t in tools}
    if "set_plan" in by:
        p = by["set_plan"].get("parameters") or {}
        props = p.get("properties") or {}
        m = DEFAULT.search(text)
        letters = [] if not m or m.group(1).strip() == "nothing" else [x.strip() for x in m.group(1).split(",")]
        allowed = set(((props.get("open") or {}).get("items") or {}).get("enum") or [])
        hints = {"open": [x for x in letters if x in allowed], "end_call": "none", "report": "none", "because": "none",
                 "reason": "Code's plan covers what is owed, and nothing here asks for another."}
        return "plan", {"name": "set_plan", "arguments": fill(p, hints)}
    if "set_owner_wish" in by:
        low = text.lower()
        wish = ("keep_battery_full" if "battery" in low else "keep_house_as_is" if "chiller" in low or "cooling" in low else
                "keep_car_ready" if re.search(r"\bcar\b", low) else None)
        if wish:
            return "note", {"name": "set_owner_wish", "arguments": {"wish": wish, "by": "", "until": "tonight" if "tonight" in low else ""}}
        return "note", {"name": "no_wish", "arguments": {}}
    if "file_report" in by:
        return "report", {"name": "file_report", "arguments": {"summary": "The sale ran as the plan said. Nothing went wrong that the facts show."}}
    if "set_zone_cooling" in by:
        m = PRECOOL.search(text)
        p = by["set_zone_cooling"].get("parameters") or {}
        hints = {k: float(m.group(2)) for k in (p.get("properties") or {}) if "offset" in k} if m else {}
        return "pre-cool", {"name": "set_zone_cooling", "arguments": fill(p, hints)}
    if "no_action" in by:
        return "nothing", {"name": "no_action", "arguments": {**fill(by["no_action"].get("parameters") or {}, {}), "reason": "Nothing here needs a change, so code's plan stands."}}
    f = next(iter(by.values()))
    return "first", {"name": f["name"], "arguments": fill(f.get("parameters") or {}, {"reason": "stand-in"})}


class H(BaseHTTPRequestHandler):
    def log_message(self, *x):
        return

    def reply(self, o):
        b = json.dumps(o).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        self.reply({"models": [{"name": a.name, "digest": "stand-in"}], "version": "stand-in", "chat": count["chat"]})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
        t0 = time.time()
        time.sleep(a.ms / 1000)
        text = " ".join(str(m.get("content") or "") for m in body.get("messages") or [])
        if body.get("tools"):
            kind, call = with_tools(body["tools"], text)
            m = {"role": "assistant", "content": "", "tool_calls": [{"function": call}]}
        elif isinstance(body.get("format"), dict):
            kind, ans = structured(body["format"], text)
            m = {"role": "assistant", "content": json.dumps(ans)}
        else:
            kind, m = "text", {"role": "assistant", "content": "The sale ran as the plan said."}
        with lock:
            count["chat"] += 1
            count[kind] = count.get(kind, 0) + 1
            if a.log:
                with open(a.log, "a") as f:
                    f.write(json.dumps({"wall": round(t0, 3), "kind": kind, "node": node_of(text), "ms": round(1000 * (time.time() - t0))}) + "\n")
        self.reply({"message": m, "prompt_eval_count": 100, "eval_count": 10, "done": True})


ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
