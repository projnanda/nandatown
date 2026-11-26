"""Run the Atlas clean control and both attacks against all three defenses."""
import argparse
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import threading
from datetime import datetime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, default=None)
    parser.add_argument('--model-dir', default='models/piguard-dd78b24e3301')
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    os.chdir(repo)
    if not os.environ.get('OPENAI_API_KEY'):
        parser.error('Set OPENAI_API_KEY before running the experiments')
    if not Path(args.model_dir).is_dir():
        parser.error('PIGuard model directory does not exist')
    output = args.out or Path('runs') / ('atlas-variants-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    if output.exists():
        parser.error('Output directory already exists')
    output.mkdir(parents=True)
    fixture = repo / 'tests/fixtures/web_evidence/atlas_relay'
    question = (fixture / 'task.txt').read_text(encoding='utf-8').strip()
    server = ThreadingHTTPServer(('127.0.0.1', 0), partial(
        SimpleHTTPRequestHandler, directory=str(fixture / 'site')))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        for variant in ('clean', 'malicious', 'malicious_v2'):
            for defense in ('baseline', 'llm', 'piguard'):
                name = f'{variant}-{defense}'
                print(f'Running {name}', flush=True)
                command = [sys.executable, '-m', 'nandatown.cli', 'web-evidence',
                    '--defense', defense, '--url',
                    f'http://127.0.0.1:{server.server_port}/{variant}/index.html',
                    '--question', question, '--out', str(output / name), '--json']
                if defense == 'piguard':
                    command += ['--model-dir', args.model_dir]
                result = subprocess.run(command)
                if result.returncode:
                    print(f'Stopped after {name} failed (exit {result.returncode}).', file=sys.stderr)
                    return result.returncode
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
