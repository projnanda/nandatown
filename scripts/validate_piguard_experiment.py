"""Gated real classifier check, two Agent smokes, then 20 PIGuard-only runs."""
from __future__ import annotations

import argparse
import hashlib
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
import threading


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model-dir', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    args = parser.parse_args()
    if args.out.exists():
        parser.error('Output must be new; prior results are never overwritten')
    args.out.mkdir(parents=True)
    repo = Path(__file__).resolve().parents[1]
    site = repo / 'tests/fixtures/web_evidence/atlas_relay/site'
    sources = [repo / 'src/nandatown' / name for name in
               ('piguard_guard.py', 'piguard_workflow.py', 'evidence_workflow.py', 'web_evidence.py')]
    sources += [site / variant / 'index.html' for variant in ('clean', 'malicious')]
    save(args.out / 'source-hashes.json', {str(p.relative_to(repo)):
         hashlib.sha256(p.read_bytes()).hexdigest() for p in sources})
    with (args.out / 'environment-freeze.txt').open('w', encoding='utf-8') as stream:
        subprocess.run([sys.executable, '-m', 'pip', 'freeze', '--exclude-editable'],
                       stdout=stream, check=True)
    summary = []
    offline_env = os.environ.copy()
    for name in ('OPENAI_API_KEY', 'DEEPSEEK_API_KEY'):
        offline_env.pop(name, None)
    with (args.out / 'classifier.stdout.log').open('w', encoding='utf-8') as stdout, \
            (args.out / 'classifier.stderr.log').open('w', encoding='utf-8') as stderr:
        check = subprocess.run([sys.executable, str(repo / 'scripts/check_piguard.py'),
            '--model-dir', str(args.model_dir), '--out', str(args.out / 'classifier-only')],
            env=offline_env, stdout=stdout, stderr=stderr)
    if check.returncode:
        print('STOP: classifier-only acceptance failed; no Agent API called.', flush=True)
        return 1
    print('PASS: real classifier-only Clean/Malicious, without Agent API keys.', flush=True)
    if not os.environ.get('OPENAI_API_KEY'):
        print('STOP: OPENAI_API_KEY missing after classifier-only success.', flush=True)
        return 2
    server = ThreadingHTTPServer(('127.0.0.1', 0),
        partial(SimpleHTTPRequestHandler, directory=str(site)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    question = (site.parent / 'task.txt').read_text(encoding='utf-8').strip()
    try:
        stages = [('smoke', 1), ('batch', 10)]
        for stage, repeats in stages:
            for repeat in range(1, repeats + 1):
                for variant in ('clean', 'malicious'):
                    name = f'{stage}-{variant}-{repeat:02d}'
                    output = args.out / name
                    command = [sys.executable, '-m', 'nandatown.cli', 'web-evidence',
                        '--defense', 'piguard', '--provider', 'openai', '--model', 'gpt-4.1',
                        '--model-dir', str(args.model_dir), '--question', question,
                        '--url', f'http://127.0.0.1:{server.server_port}/{variant}/index.html',
                        '--out', str(output), '--json']
                    with (args.out / f'{name}.stdout.log').open('w', encoding='utf-8') as stdout, \
                            (args.out / f'{name}.stderr.log').open('w', encoding='utf-8') as stderr:
                        completed = subprocess.run(command, stdout=stdout, stderr=stderr)
                    result = json.loads((output / 'result.json').read_text(encoding='utf-8'))
                    guard = json.loads((output / 'piguard.json').read_text(encoding='utf-8')) if (output / 'piguard.json').exists() else {}
                    route = result.get('routing') or {}
                    expected_model = 'gpt-4.1' if guard.get('detection') == 'benign' else 'gpt-6-luna'
                    passed = (completed.returncode == 0 and result['status'] == 'completed'
                        and guard.get('status') == 'complete' and not guard.get('errors')
                        and bool(guard.get('windows')) and bool(guard.get('model_identity'))
                        and route.get('reason') != 'guard_incomplete'
                        and result['models']['agent2'] == expected_model)
                    row = {'run': name, 'status': result['status'], 'guard_status': guard.get('status'),
                        'detection': guard.get('detection'), 'windows': len(guard.get('windows', [])),
                        'agent2': result['models']['agent2'], 'route_reason': route.get('reason'),
                        'error_route_used': route.get('reason') == 'guard_incomplete',
                        'answer': result.get('answer'), 'passed_runtime_gate': passed}
                    summary.append(row)
                    save(args.out / 'summary.json', summary)
                    print(json.dumps({k:v for k,v in row.items() if k != 'answer'}), flush=True)
                    if not passed:
                        print('STOP: runtime gate failed; no further paid calls.', flush=True)
                        return 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
