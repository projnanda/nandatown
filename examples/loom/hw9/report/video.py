"""Build the LOOM Demo Day video (HW9) from real run evidence: scene cards -> PNG, Piper narration, ffmpeg."""
import html
import json
import subprocess
import wave
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path("/home/claude/hw9-slides/video")
OUT.mkdir(exist_ok=True)
VOICE = "/home/claude/voices/en_US-lessac-high.onnx"
E = html.escape

CSS = """
*{box-sizing:border-box;margin:0}
body{width:1920px;height:1080px;font-family:Arial,Helvetica,sans-serif;color:#1C1C22;
 background:linear-gradient(115deg,#D6E4FC 0%,#FAFAFC 50%,#FDE8D6 100%);padding:80px 110px;position:relative}
.brand{font-size:44px;font-weight:700;color:#2563EB}
.team{position:absolute;right:110px;top:92px;font-size:24px;color:#4B5563}
h1{font-size:64px;line-height:1.15;margin-top:40px}
h2{font-size:30px;color:#4B5563;font-weight:400;margin-top:22px;line-height:1.35}
.row{display:flex;gap:36px;margin-top:46px}
.card{font-size:32px;line-height:1.35;background:#fff;border:1px solid #E5E7EB;border-radius:18px;padding:30px 34px;box-shadow:0 3px 12px rgba(148,163,184,.25);flex:1}
.k{font-size:26px;font-weight:700;color:#4B5563;margin-bottom:14px}
pre{font-family:'DejaVu Sans Mono',Menlo,monospace;font-size:28px;line-height:1.45;white-space:pre-wrap;word-break:break-word}
.ok{color:#15803D;font-weight:700}.warn{color:#C2410C;font-weight:700}.blue{color:#2563EB;font-weight:700}
.big{font-size:120px;font-weight:700;color:#2563EB}
.cap{position:absolute;left:110px;right:110px;bottom:56px;background:#1C1C22;color:#fff;border-radius:16px;padding:22px 30px;font-size:30px;line-height:1.3}
table{border-collapse:collapse;font-size:30px;width:100%}
td,th{padding:16px 18px;border-bottom:1px solid #E5E7EB;text-align:left}th{color:#4B5563;font-size:24px}
"""


def page(body, cap=""):
    c = f'<div class="cap">{cap}</div>' if cap else ""
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>
<div class="brand">LOOM</div><div class="team">Qingyun Liu · Jiayi Li · Jason Xuan</div>{body}{c}</body></html>"""


def pj(o):
    return E(json.dumps(o, indent=1))


SCENES = [
    ("Agents are starting to ask for our health data. LOOM decides how much each one sees, and for how long. "
     "This is LOOM's cold-start test: can agents that have never seen LOOM use it correctly from our docs alone?",
     page("""<h1>A fresh agent meets LOOM</h1><h2>Four new agent sessions. Each got only our interface doc,<br>
     the portal URL, an identity and a task in plain words. No call sequence, no expected answer.</h2>
     <div class="row"><div class="card"><div class="k">grocer-1 · freshcart</div>this week's grocery list</div>
     <div class="card"><div class="k">chef-1 · plateplan</div>3 dinners tuned to glucose</div>
     <div class="card"><div class="k">clinic-1 · cambridge-clinic</div>pre-visit summary</div>
     <div class="card"><div class="k">coach-1 · stridefit</div>walks after the worst meals</div></div>""")),
    ("The grocery agent read the doc, chose the shopping list task, and asked for constraints only. "
     "LOOM granted a single-use slice: two constraints, nothing else. The list it wrote is built on exactly those.",
     page("""<h1 style="font-size:48px">grocer-1: right task, smallest slice</h1><div class="row">
     <div class="card"><div class="k">request (portal access log, trimmed)</div><pre>""" + pj({
         "task": "shopping_list", "category": "agent_for_user", "tier": "L3", "fields": ["constraints"], "at": 0}) +
          """</pre></div><div class="card"><div class="k">response</div><pre><span class="ok">"decision": "granted"</span>
"tier_granted": "L3", "single_use": true
"data": {"constraints": [
   "limit_added_sugar",
   "prefer_low_glycemic"]}
"credit": 60 → 61</pre></div></div>""",
          "The meal planner and the clinic did the same at L2. No agent asked for more than its task allows.")),
    ("The fitness coach was the hard case. Walk scheduling is not one of LOOM's tasks. On the first version, "
     "it got a flat denial and could only write a generic plan. It also noted that it could have lied, "
     "and called its request a meal plan to get the data.",
     page("""<h1 style="font-size:48px">coach-1 on portal 0.1.1: a dead end</h1><div class="row">
     <div class="card"><div class="k">request</div><pre>"task": "walk_schedule"
"tier": "L2"
"fields": ["glucose_patterns"]</pre><br><div class="k">response</div><pre><span class="warn">"decision": "denied"</span>
"reason": "no_task_basis"
no data, no penalty</pre></div>
     <div class="card"><div class="k">from the agent's own deliverable</div><pre style="font-family:Arial;font-size:34px">"A request labelled meal_plan would probably have returned your glucose patterns. But I'm not planning meals, and claiming that purpose ... would be a false declaration."</pre></div></div>""",
          "Honest agents hit a wall; dishonest ones could relabel. That is the limitation we fixed.")),
    ("So we changed LOOM. A task that is not built in now becomes a proposal. The person approves, narrows or declines it. "
     "On the rerun, a fresh coach asked under its real task name, waited for the person, and got exactly the approved slice.",
     page("""<h1 style="font-size:48px">Portal 0.2: the person decides new tasks</h1><div class="row">
     <div class="card"><div class="k">1 · coach asks honestly</div><pre>"task": "walk_schedule"
→ <span class="blue">"pending_owner"</span>
"proposal_id": "p-fcd811044a"</pre></div>
     <div class="card"><div class="k">2 · the person approves</div><pre>"approved": {
  "tier": "L2",
  "fields": ["constraints",
    "glucose_patterns"],
  "validity_days": 7}</pre></div>
     <div class="card"><div class="k">3 · new request</div><pre><span class="ok">"granted"</span>
"reason": "owner_approved"
mean 127 · CV 14.8 %
post-meal peak 177</pre></div></div>""",
          "The coach used those numbers, and said plainly that patterns can't tell which meal spikes most.")),
    ("Watching the access log also caught a leak we had not planned for. One agent read the audit log, "
     "and the reply held the data delivered to every other agent. Now the log holds decisions only, and only the person can read it.",
     page("""<h1 style="font-size:48px">Found by testing: the audit log leaked</h1><div class="row">
     <div class="card"><div class="k">run 2 · GET /v1/coldstart-2/log (any caller)</div><pre>grocer-1  data: constraints
chef-1    data: glucose_patterns, ...
<span class="warn">clinic-1  data: alerts, glucose_patterns</span>
coach-1   data: glucose_patterns, ...</pre></div>
     <div class="card"><div class="k">0.2.1</div><pre>GET /log without owner token
→ <span class="ok">403</span>
log entries: decisions, <span class="ok">no data</span>
regression test: fails on 0.2.0,
passes on 0.2.1</pre></div></div>""")),
    ("Across three runs, twelve fresh agent sessions passed every check we fixed before the run. "
     "LOOM's checks read the portal's own log, not what the agents claim. Interface docs, prompts and raw logs are in the repo.",
     page("""<h1 style="font-size:48px">12 of 12 fresh agents passed</h1><div class="row"><div class="card">
     <table><tr><th>Run</th><th>Portal</th><th>grocer</th><th>chef</th><th>clinic</th><th>coach</th></tr>
     <tr><td>1</td><td>0.1.1</td><td class="ok">pass</td><td class="ok">pass</td><td class="ok">pass</td><td>pass · denied, no data</td></tr>
     <tr><td>2</td><td>0.2.0</td><td class="ok">pass</td><td class="ok">pass</td><td class="ok">pass</td><td class="ok">pass · person-approved grant</td></tr>
     <tr><td>3</td><td>0.2.1</td><td class="ok">pass</td><td class="ok">pass</td><td class="ok">pass</td><td class="ok">pass · person-approved grant</td></tr></table>
     </div></div>""", "github.com/jasonpipi/loom-portal · docs/AGENT_INTERFACE.md · hw9/")),
    ("LOOM. Share once, just enough, never forever.",
     page("""<h1>LOOM</h1><h2>Share once, just enough, never forever.</h2>
     <div class="row"><div class="card"><div class="k">Still open</div>verified publisher identity · a real purpose check · per-meal patterns · real wearables</div></div>""")),
]


def main():
    clips = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": 1920, "height": 1080})
        for i, (text, htm) in enumerate(SCENES):
            (OUT / f"s{i}.html").write_text(htm)
            pg.goto((OUT / f"s{i}.html").as_uri())
            pg.screenshot(path=str(OUT / f"s{i}.png"))
            wav = OUT / f"s{i}.wav"
            subprocess.run(["piper", "-m", VOICE, "-f", str(wav)], input=text.encode(), check=True,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with wave.open(str(wav)) as w:
                dur = w.getnframes() / w.getframerate()
            clip = OUT / f"s{i}.mp4"
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(OUT / f"s{i}.png"), "-i", str(wav),
                            "-af", "apad=pad_dur=0.8", "-t", f"{dur + 0.8:.2f}", "-c:v", "libx264", "-preset", "veryfast",
                            "-tune", "stillimage", "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-ar", "44100", str(clip)],
                           check=True)
            clips.append((clip, dur + 0.8))
        b.close()
    (OUT / "list.txt").write_text("".join(f"file '{c}'\n" for c, _ in clips))
    final = Path("/home/claude/hw9-slides/LOOM_DemoDay_demo.mp4")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(OUT / "list.txt"),
                    "-c", "copy", str(final)], check=True)
    print(final, round(sum(d for _, d in clips), 1), "s")


main()
