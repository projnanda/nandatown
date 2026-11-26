"""Real local classifier acceptance check, with Chromium but without Agent APIs."""
from __future__ import annotations

import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import version
import json
from pathlib import Path
import sys
import threading

from nandatown.piguard_guard import inspect_packet, digest
from nandatown.web_evidence import EvidenceItem, EvidencePacket, PlaywrightBrowser, RunConfig


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--site-root', type=Path, default=Path(__file__).resolve().parents[1] /
                        'tests/fixtures/web_evidence/atlas_relay/site')
    args = parser.parse_args()
    if args.out.exists():
        parser.error('Output must not already exist')
    args.out.mkdir(parents=True)
    environment = {'python': sys.version, 'executable': sys.executable,
                   'packages': {name: version(name) for name in
                       ('torch', 'transformers', 'tokenizers', 'safetensors', 'numpy', 'psutil')},
                   'agent_api_calls': 0}
    (args.out / 'environment.json').write_text(json.dumps(environment, indent=2), encoding='utf-8')
    server = ThreadingHTTPServer(('127.0.0.1', 0),
        partial(SimpleHTTPRequestHandler, directory=str(args.site_root.resolve())))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    summary = []
    try:
        for variant, expected in [('clean', 'benign'), ('malicious', 'injection_detected')]:
            url = f'http://127.0.0.1:{server.server_port}/{variant}/index.html'
            browser = PlaywrightBrowser(RunConfig(url=url, question='Local classifier check'))
            try:
                browser.start()
                page = browser.open_page(url)
            finally:
                browser.close()
            if page.snapshot_truncated:
                raise RuntimeError('Fixture extraction was truncated')
            packet = EvidencePacket(question='Local classifier check', complete=True, items=[
                EvidenceItem(evidence_id='E1', page_id=page.page_id, url=url,
                    text=page.visible_text, selector='body', block_id='PAGE_VISIBLE_TEXT',
                    start_offset=0, end_offset=len(page.visible_text),
                    source_sha256='sha256:' + digest(page.visible_text))])
            (args.out / f'{variant}-packet.json').write_text(packet.model_dump_json(indent=2), encoding='utf-8')
            report = inspect_packet(packet, args.model_dir)
            (args.out / f'{variant}-piguard.json').write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            row = {'variant': variant, 'expected': expected, 'status': report['status'],
                   'detection': report['detection'], 'windows': len(report['windows']),
                   'errors': report['errors'], 'official_model_loaded': 'model_identity' in report,
                   'passed': report['status'] == 'complete' and report['detection'] == expected
                             and bool(report['windows']) and 'model_identity' in report}
            summary.append(row)
            print(json.dumps(row), flush=True)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        (args.out / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    return 0 if len(summary) == 2 and all(row['passed'] for row in summary) else 1


if __name__ == '__main__':
    raise SystemExit(main())
