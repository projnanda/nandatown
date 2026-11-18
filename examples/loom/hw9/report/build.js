const pptxgen = require("pptxgenjs");
const { applyTheme } = require("/root/.claude/skills/synced/bfb082d1-43f4-4188-ad15-367e5e63e2ad_7b2355f1-556b-4eae-be25-1c7a1b446c18/pptx/scripts/apply_theme.js");

const THEME = {
  name: "LOOM Demo Day",
  headFontFace: "Arial",
  bodyFontFace: "Arial",
  colors: {
    dk1: "1C1C22", lt1: "FFFFFF", dk2: "4B5563", lt2: "F3F4F6",
    accent1: "2563EB", accent2: "C2410C", accent3: "15803D", accent4: "B91C1C",
    accent5: "93C5FD", accent6: "FDBA74", hlink: "2563EB", folHlink: "1D4ED8",
  },
};

const pres = new pptxgen();
pres.layout = "LAYOUT_16x9"; // 10 x 5.625
pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
pres.title = "LOOM - Demo Day";
const C = pres.SchemeColor;
const BLUE = C.accent1, ORANGE = C.accent2, INK = C.text1, MUTED = C.text2;

pres.defineSlideMaster({
  title: "LOOM_CONTENT",
  background: { path: "bg.png" },
  objects: [
    { text: { text: "LOOM", options: { x: 0.45, y: 0.28, w: 1.6, h: 0.42, fontSize: 22, bold: true, color: BLUE, margin: 0, isTextBox: true } } },
    { text: { text: "Qingyun Liu · Jiayi Li · Jason Xuan", options: { x: 5.6, y: 0.3, w: 3.95, h: 0.3, fontSize: 10, color: MUTED, align: "right", margin: 0, isTextBox: true } } },
  ],
  slideNumber: { x: 9.3, y: 5.3, w: 0.4, h: 0.2, fontSize: 9, color: "6B7280" },
});

function card(slide, x, y, w, h, name) {
  slide.addShape(pres.shapes.ROUNDED_RECTANGLE, {
    x, y, w, h, rectRadius: 0.08, fill: { color: "FFFFFF" }, line: { color: "E5E7EB", width: 0.75 },
    shadow: { type: "outer", color: "94A3B8", opacity: 0.18, blur: 6, offset: 1.5, angle: 90 }, objectName: name,
  });
}
function T(slide, text, o) { slide.addText(text, Object.assign({ isTextBox: true, margin: 0, color: INK, valign: "top" }, o)); }
function num(slide, n, x, y) {
  slide.addShape(pres.shapes.OVAL, { x, y, w: 0.34, h: 0.34, fill: { color: "DBEAFE" }, line: { type: "none" } });
  T(slide, n, { x, y: y + 0.005, w: 0.34, h: 0.33, fontSize: 11, bold: true, color: BLUE, align: "center", valign: "middle" });
}

// ---------------- Slide 1: problem, why agentic, users ----------------
{
  const s = pres.addSlide({ masterName: "LOOM_CONTENT" });
  T(s, "Agents ask for your health data.\nLOOM decides how much they see, and for how long.", { x: 0.45, y: 0.85, w: 9.1, h: 0.95, fontSize: 24, bold: true });
  T(s, "LOOM issues data visas to AI agents: one task, only the detail that task needs, and every visa expires.", { x: 0.45, y: 1.85, w: 9.1, h: 0.35, fontSize: 13, color: MUTED });

  const cols = [
    ["The problem", "A meal-planning agent, a grocery agent and a clinic assistant all want your glucose data. Today one tap gives each of them everything, forever, and you can't see who took what."],
    ["Why it has to be agentic", "The askers are agents, at machine speed. They need an answer they can act on (granted, which fields, until when), not a consent screen built for a person."],
    ["Who uses it", "Agents that work for a person (grocery, meal plan, fitness), care providers' agents, and the person, who approves anything new and reads the audit log."],
  ];
  cols.forEach(([h, b], i) => {
    const x = 0.45 + i * 3.08;
    card(s, x, 2.4, 2.9, 2.05, `card_${i}`);
    T(s, h, { x: x + 0.2, y: 2.55, w: 2.5, h: 0.3, fontSize: 14, bold: true, color: i === 0 ? ORANGE : BLUE });
    T(s, b, { x: x + 0.2, y: 2.92, w: 2.5, h: 1.45, fontSize: 11.5, color: INK, lineSpacingMultiple: 1.1 });
  });
  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 0.45, y: 4.62, w: 9.1, h: 0.55, rectRadius: 0.08, fill: { color: "1C1C22" }, line: { type: "none" }, objectName: "tiers" });
  T(s, [
    { text: "Tiers   ", options: { bold: true, color: "FFFFFF" } },
    { text: "L0 raw signal  ·  L1 readings  ·  L2 patterns  ·  L3 constraints", options: { color: "E5E7EB" } },
    { text: "      the task sets the deepest tier", options: { color: "FDBA74", italic: true } },
  ], { x: 0.7, y: 4.62, w: 8.7, h: 0.55, fontSize: 12, valign: "middle" });
  s.addNotes("30 s. Problem: agents ask for personal health data and get all of it forever. LOOM gives each agent a task-scoped, expiring slice. Agentic because the requesters are agents and need machine-actionable answers. Users: agents working for a person, care providers, and the person.");
}

// ---------------- Slide 2: architecture / what we built ----------------
{
  const s = pres.addSlide({ masterName: "LOOM_CONTENT" });
  T(s, "How LOOM works", { x: 0.45, y: 0.85, w: 9, h: 0.5, fontSize: 24, bold: true });

  // left: agents
  const agents = [["grocer", "shopping list → L3"], ["meal planner", "meal plan → L2"], ["clinic", "clinical visit → L2"], ["fitness coach", "not built in → person decides"]];
  T(s, "Agents (any builder)", { x: 0.45, y: 1.45, w: 2.3, h: 0.25, fontSize: 11, bold: true, color: MUTED });
  agents.forEach(([a, t], i) => {
    const y = 1.78 + i * 0.68;
    card(s, 0.45, y, 2.3, 0.56, `agent_${i}`);
    T(s, a, { x: 0.6, y: y + 0.07, w: 2.05, h: 0.22, fontSize: 11.5, bold: true });
    T(s, t, { x: 0.6, y: y + 0.3, w: 2.05, h: 0.2, fontSize: 9.5, color: i === 3 ? ORANGE : MUTED });
  });

  // arrows
  s.addShape(pres.shapes.LINE, { x: 2.85, y: 2.95, w: 0.65, h: 0, line: { color: "6B7280", width: 1.5, endArrowType: "triangle" } });
  T(s, "HTTP / JSON\nor Nanda Town", { x: 2.8, y: 3.05, w: 0.8, h: 0.4, fontSize: 8, color: MUTED, align: "center" });

  // center: portal
  card(s, 3.6, 1.45, 3.25, 3.1, "portal");
  T(s, "LOOM portal", { x: 3.8, y: 1.58, w: 2.9, h: 0.3, fontSize: 14, bold: true, color: BLUE });
  const steps = [
    ["Check task → tier", "asking deeper or extra = blocked, −20"],
    ["Grant the shallowest slice", "only the task's fields, with an expiry"],
    ["Credit sets friction, not depth", "how often the person confirms"],
    ["New task? Ask the person", "proposal → approve, narrow or decline"],
  ];
  steps.forEach(([h, b], i) => {
    const y = 1.98 + i * 0.62;
    num(s, String(i + 1), 3.8, y);
    T(s, h, { x: 4.25, y: y - 0.01, w: 2.5, h: 0.22, fontSize: 11, bold: true });
    T(s, b, { x: 4.25, y: y + 0.21, w: 2.5, h: 0.3, fontSize: 9.5, color: MUTED });
  });

  s.addShape(pres.shapes.LINE, { x: 6.95, y: 2.95, w: 0.55, h: 0, line: { color: "6B7280", width: 1.5, beginArrowType: "triangle", endArrowType: "triangle" } });

  // right: person + record
  card(s, 7.6, 1.45, 1.95, 1.35, "person");
  T(s, "The person", { x: 7.75, y: 1.58, w: 1.7, h: 0.25, fontSize: 12, bold: true, color: ORANGE });
  T(s, "approves new tasks, reads the audit log (owner token)", { x: 7.75, y: 1.88, w: 1.7, h: 0.8, fontSize: 9.5, color: MUTED });
  card(s, 7.6, 3.0, 1.95, 1.55, "record");
  T(s, "Health record", { x: 7.75, y: 3.13, w: 1.7, h: 0.25, fontSize: 12, bold: true });
  T(s, "synthetic: 14 days of glucose, patterns, alerts, constraints", { x: 7.75, y: 3.43, w: 1.7, h: 0.9, fontSize: 9.5, color: MUTED });

  s.addShape(pres.shapes.ROUNDED_RECTANGLE, { x: 0.45, y: 4.72, w: 9.1, h: 0.45, rectRadius: 0.08, fill: { color: "1C1C22" }, line: { type: "none" } });
  T(s, [
    { text: "Built:  ", options: { bold: true, color: "FFFFFF" } },
    { text: "LOOM portal (Python, stdlib only) · agent interface doc · Nanda Town gateway + 11 validators (PR #362) · 19 portal tests", options: { color: "E5E7EB" } },
  ], { x: 0.7, y: 4.72, w: 8.7, h: 0.45, fontSize: 10.5, valign: "middle" });
  s.addNotes("30 s. Agents send a task, tier and fields. The portal checks the task, grants the shallowest slice with an expiry, and penalizes overreach. Credit changes friction, never depth. A task that is not built in goes to the person as a proposal. Runs over plain HTTP and inside Nanda Town through a gateway.");
}

// ---------------- Slide 3: evidence HW6-HW9 ----------------
{
  const s = pres.addSlide({ masterName: "LOOM_CONTENT" });
  T(s, "What the evidence shows", { x: 0.45, y: 0.85, w: 9, h: 0.5, fontSize: 24, bold: true });
  const stats = [
    ["HW6 · MVP", "8 / 8", "checks passed end to end: six agents rebalance a packed day; plan v2 executed and read back"],
    ["HW7 · benchmark", "12 vs 9", "successes out of 15, LOOM vs copy-passing. The planner got 0 biometric records under LOOM vs 4"],
    ["HW8 · Nanda Town", "11 / 11", "validators passed, incl. overreach → Town reputation down; portal down → every request denied"],
    ["HW9 · cold start", "12 / 12", "fresh agents passed with only our docs. Testing found 2 problems; both fixed and retested"],
  ];
  stats.forEach(([h, big, b], i) => {
    const x = 0.45 + i * 2.31;
    card(s, x, 1.5, 2.16, 2.55, `stat_${i}`);
    T(s, h, { x: x + 0.18, y: 1.65, w: 1.85, h: 0.25, fontSize: 10.5, bold: true, color: MUTED });
    T(s, big, { x: x + 0.18, y: 1.95, w: 1.85, h: 0.7, fontSize: 32, bold: true, color: i === 3 ? ORANGE : BLUE });
    T(s, b, { x: x + 0.18, y: 2.72, w: 1.85, h: 1.45, fontSize: 10.5, lineSpacingMultiple: 1.08 });
  });
  card(s, 0.45, 4.25, 9.1, 0.78, "honest");
  T(s, [
    { text: "Limits we measured, not hid: ", options: { bold: true, color: ORANGE } },
    { text: "HW7 C4 failed in both modes (0/3); small samples (3 runs per cell). HW9 agents were fresh sessions we started, not another team's agents. LOOM checks tier and fields, not true purpose.", options: { color: INK } },
  ], { x: 0.65, y: 4.33, w: 8.7, h: 0.62, fontSize: 10.5, valign: "middle" });
  s.addNotes("40 s. One number per homework. HW6 the core runs. HW7 LOOM beats copy-passing and shares less. HW8 it plugs into Nanda Town and fails closed. HW9 agents that never saw LOOM used it correctly from the docs alone, and testing found two real problems we fixed.");
}

// ---------------- Slide 4: demo + worked / failed / next ----------------
{
  const s = pres.addSlide({ masterName: "LOOM_CONTENT" });
  T(s, "Demo: a fresh agent meets LOOM", { x: 0.45, y: 0.85, w: 9, h: 0.5, fontSize: 24, bold: true });
  // video slot: a still from the demo; in Google Slides put the YouTube video here (Play automatically)
  s.addImage({ path: "thumb.png", x: 0.45, y: 1.5, w: 5.2, h: 2.925, objectName: "video_slot", altText: "Still from the LOOM demo video" });
  s.addShape(pres.shapes.OVAL, { x: 2.68, y: 2.59, w: 0.75, h: 0.75, fill: { color: "1C1C22", transparency: 20 }, line: { type: "none" } });
  s.addShape(pres.shapes.ISOSCELES_TRIANGLE, { x: 2.94, y: 2.76, w: 0.35, h: 0.4, rotate: 90, fill: { color: "FFFFFF" }, line: { type: "none" } });
  T(s, process.env.YT ? "Demo video: " + process.env.YT : "Demo video (85 s): YouTube link in the deck", { x: 0.45, y: 4.47, w: 5.2, h: 0.2, fontSize: 9, color: MUTED });
  const blocks = [
    ["Worked", BLUE, "12 fresh agents used LOOM from the doc alone: right task, exactly the allowed fields, data actually used."],
    ["Failed, then fixed", ORANGE, "Unsupported task was a dead end; the audit log leaked other agents' data. Fixed: person approves new tasks; log is owner-only."],
    ["Next", C.accent3, "Verified publisher identity, a purpose check beyond tier and fields, per-meal patterns, real wearables."],
  ];
  blocks.forEach(([h, col, b], i) => {
    const y = 1.5 + i * 1.0;
    card(s, 5.85, y, 3.7, 0.9, `wfn_${i}`);
    T(s, h, { x: 6.03, y: y + 0.08, w: 3.4, h: 0.22, fontSize: 11.5, bold: true, color: col });
    T(s, b, { x: 6.03, y: y + 0.32, w: 3.4, h: 0.55, fontSize: 9.5 });
  });
  T(s, "Interface doc, runs and code: github.com/jasonpipi/loom-portal  ·  Nanda Town PR projnanda/nandatown#362", { x: 0.45, y: 4.85, w: 9.1, h: 0.3, fontSize: 9.5, color: MUTED });
  s.addNotes("40 s. Play the video: a fresh fitness-coach agent reads the doc, asks under its real task name, waits for the person's approval, and gets only the approved slice. Then: what worked, what failed and how we fixed it, and what is next.");
}

(async () => {
  await pres.writeFile({ fileName: "LOOM_DemoDay_slides.pptx" });
  await applyTheme("LOOM_DemoDay_slides.pptx", THEME);
  console.log("ok");
})();
