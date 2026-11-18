"""Build hw9/report/LOOM_HW9_usability.pdf = report.html (+ interface doc) + Demo Day slides PDF."""
import subprocess, sys
from pathlib import Path
import markdown
from playwright.sync_api import sync_playwright

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
SLIDES = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "LOOM_DemoDay_slides.pdf"
doc = markdown.markdown((ROOT / "AGENT_INTERFACE.md").read_text(), extensions=["tables", "fenced_code"])
doc = doc.replace("<h1>", "<h3>").replace("</h1>", "</h3>")
html = (HERE / "report.html").read_text().replace('<div id="doc"></div>', doc)
(HERE / "_full.html").write_text(html)
with sync_playwright() as p:
    b = p.chromium.launch(); pg = b.new_page()
    pg.goto((HERE / "_full.html").as_uri()); pg.wait_for_load_state("networkidle")
    pg.pdf(path=str(HERE / "_report.pdf"), format="Letter", print_background=True, display_header_footer=True,
           header_template="<span></span>", margin={"top": "15mm", "bottom": "16mm", "left": "15mm", "right": "15mm"},
           footer_template="<div style='font-size:7px;color:#888;width:100%;text-align:center;font-family:sans-serif'>"
                           "LOOM · HW9 agent usability · <span class='pageNumber'></span>/<span class='totalPages'></span></div>")
    b.close()
# Appendix B title page is the slides themselves (landscape pages)
from pypdf import PdfReader, PdfWriter
w = PdfWriter()
for f in (HERE / "_report.pdf", SLIDES):
    for page in PdfReader(str(f)).pages:
        w.add_page(page)
out = HERE / "LOOM_HW9_usability.pdf"
w.write(str(out)); print(out, len(w.pages), "pages")
