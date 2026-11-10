"""Bounded Town Lab entry point for the TermsMet integration."""
from __future__ import annotations

import argparse
from contextlib import suppress
import importlib.util
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


HERE = Path(__file__).resolve().parent
TOWN_ROOT = HERE.parents[2]
PLUGIN = HERE / 'termsmet_plugin.py'
SCENARIOS = {'clean': 'termsmet_workflow.yaml', 'lost-reply': 'termsmet_lost_reply.yaml'}
VALIDATORS = {'clean': 'termsmet_clean', 'lost-reply': 'termsmet_lost_reply'}
BASE_STAGES = {'agreement_funding', 'delivery_repair', 'release_gating', 'retrieved_artifact'}
GRACE_SECONDS = 5


class RunnerError(Exception):
    pass


class Interrupted(BaseException):
    pass


def _load_plugin(path):
    from nandatown.sim.validators import VALIDATORS
    if not path.is_file():
        raise RunnerError('required TermsMet plugin is missing')
    before = dict(VALIDATORS)
    spec = importlib.util.spec_from_file_location('termsmet_runner_plugin', path)
    if spec is None or spec.loader is None:
        raise RunnerError('cannot load TermsMet plugin')
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise RunnerError('cannot import TermsMet plugin') from exc
    if not callable(getattr(module, 'close_termsmet', None)):
        raise RunnerError('plugin must define close_termsmet()')
    registered = all(name in VALIDATORS and VALIDATORS[name] is not before.get(name)
                     for name in VALIDATORS_REQUIRED)
    return module, registered


VALIDATORS_REQUIRED = frozenset(VALIDATORS.values())


def _check_result(result, case):
    required = BASE_STAGES | ({'lost_reply_recovery'} if case == 'lost-reply' else set())
    stages = result.stages
    names = [stage.name for stage in stages]
    if len(names) != len(set(names)) or result.verdict != 'passed':
        raise RunnerError('scenario verdict or stages did not pass')
    statuses = {stage.name: stage.status for stage in stages}
    if any(statuses.get(name) != 'passed' for name in required):
        raise RunnerError('required project stages did not pass')


def _verify_one(directory, case):
    from nandatown.bundle import load_bundle, verify_bundle
    problems = verify_bundle(str(directory))
    if problems:
        raise RunnerError('bundle verification failed')
    bundle = load_bundle(str(directory))
    if bundle['mode'] != 'lab' or bundle['profile'].validator != VALIDATORS[case]:
        raise RunnerError('bundle validator does not match case')
    _check_result(bundle['result'], case)


def _verify(root, plugin=PLUGIN):
    _module, registered = _load_plugin(Path(plugin))
    if not registered:
        raise RunnerError('plugin did not register required validators')
    for case, folder in (('clean', 'clean'), ('lost-reply', 'lost_reply')):
        directory = Path(root) / folder
        if not directory.is_dir() or directory.is_symlink():
            raise RunnerError(f'missing {case} bundle')
        _verify_one(directory, case)


def _worker(config):
    from nandatown.sim.runner import run_lab
    from nandatown.sim.scenario import load_scenario_file
    plugin = None
    category = 'run_failed'
    bundle_name = None
    def interrupt(_signum, _frame):
        raise Interrupted()
    signal.signal(signal.SIGTERM, interrupt)
    signal.signal(signal.SIGINT, interrupt)
    try:
        plugin, registered = _load_plugin(Path(config['plugin']))
        if not registered:
            raise RunnerError('plugin did not register required validators')
        scenario = load_scenario_file(config['scenario'])
        if scenario.plugin_files or scenario.validator != VALIDATORS[config['case']]:
            raise RunnerError('scenario plugin files or validator are invalid')
        bundle, result = run_lab(config['scenario'], config['pending'], seed=42)
        _check_result(result, config['case'])
        _verify_one(bundle, config['case'])
        bundle_name = Path(bundle).name
        category = 'ok'
    except Interrupted:
        category = 'interrupted'
    except BaseException:
        category = 'run_failed'
    finally:
        if plugin is not None:
            try:
                plugin.close_termsmet()
            except BaseException:
                category = 'cleanup_failed'
        Path(config['status']).write_text(json.dumps({'status': category, 'bundle': bundle_name}), encoding='utf-8')
    return 0 if category == 'ok' else 1


def _process_table():
    if sys.platform not in ('darwin', 'linux'):
        raise RunnerError('process cleanup supports macOS and Linux only')
    try:
        result = subprocess.run(['ps', '-axo', 'pid=,ppid=,lstart=,stat='], capture_output=True,
                                text=True, timeout=3, check=True)
        table = {}
        for line in result.stdout.splitlines():
            fields = line.split()
            if len(fields) >= 8:
                table[int(fields[0])] = (int(fields[1]), ' '.join(fields[2:7]), fields[7])
        return table
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        raise RunnerError('cannot inspect trial-owned processes') from exc


def _observe(worker_pid, owned):
    table = _process_table()
    parents = {worker_pid}
    changed = True
    while changed:
        changed = False
        for pid, (ppid, started, _state) in table.items():
            if ppid in parents and pid not in parents:
                parents.add(pid)
                owned[pid] = started
                changed = True
    return table


def _live_owned(owned, table):
    return [pid for pid, started in owned.items()
            if pid in table and table[pid][1] == started and not table[pid][2].startswith('Z')]


def _signal_owned(pids, sig):
    for pid in pids:
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            pass
        except PermissionError as exc:
            raise RunnerError('cannot terminate trial-owned process') from exc


def _supervise(worker, timeout):
    owned = {}
    deadline = time.monotonic() + timeout
    interrupted = False
    old = {}
    def signal_handler(_sig, _frame):
        nonlocal interrupted
        interrupted = True
    for sig in (signal.SIGINT, signal.SIGTERM):
        old[sig] = signal.signal(sig, signal_handler)
    try:
        while worker.poll() is None and not interrupted and time.monotonic() < deadline:
            _observe(worker.pid, owned)
            time.sleep(0.05)
        timed_out = not interrupted and time.monotonic() >= deadline
        if worker.poll() is None:
            _observe(worker.pid, owned)
            with suppress(ProcessLookupError):
                worker.send_signal(signal.SIGTERM)
        grace = time.monotonic() + GRACE_SECONDS
        while worker.poll() is None and time.monotonic() < grace:
            _observe(worker.pid, owned)
            time.sleep(0.05)
        table = _observe(worker.pid, owned) if worker.poll() is None else _process_table()
        survivors = _live_owned(owned, table)
        if worker.poll() is None:
            survivors.append(worker.pid)
        if survivors:
            _signal_owned(survivors, signal.SIGKILL)
        force_deadline = time.monotonic() + GRACE_SECONDS
        while time.monotonic() < force_deadline:
            worker.poll()
            if worker.returncode is not None and not _live_owned(owned, _process_table()):
                break
            time.sleep(0.05)
        if worker.poll() is None or _live_owned(owned, _process_table()):
            raise RunnerError('cleanup failed: trial-owned processes remain')
        if survivors:
            raise RunnerError('cleanup failed: forced termination required')
        if interrupted:
            raise RunnerError('run interrupted')
        if timed_out:
            raise RunnerError('run timed out')
        return worker.returncode
    except RunnerError as exc:
        if str(exc) == 'cannot inspect trial-owned processes':
            raise RunnerError('cleanup failed: cannot inspect trial-owned processes') from exc
        raise
    finally:
        try:
            # Inspection failure still unwinds the worker and tracked children.
            if worker.poll() is None:
                with suppress(ProcessLookupError):
                    worker.terminate()
                try:
                    worker.wait(timeout=GRACE_SECONDS)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        worker.kill()
                    try:
                        worker.wait(timeout=GRACE_SECONDS)
                    except subprocess.TimeoutExpired as exc:
                        raise RunnerError('cleanup failed: worker remains') from exc
            try:
                remaining = _live_owned(owned, _process_table())
            except RunnerError as exc:
                raise RunnerError('cleanup failed: cannot inspect trial-owned processes') from exc
            if remaining:
                _signal_owned(remaining, signal.SIGKILL)
                limit = time.monotonic() + GRACE_SECONDS
                while time.monotonic() < limit and _live_owned(owned, _process_table()):
                    time.sleep(0.05)
                raise RunnerError('cleanup failed: forced termination required')
        finally:
            for sig, handler in old.items():
                signal.signal(sig, handler)


def _run_live(project, project_python, case, out, *, plugin=PLUGIN, scenario=None, cwd=TOWN_ROOT,
              timeout=120):
    out = Path(out).absolute()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise RunnerError('output directory must be empty')
    if not Path(plugin).is_file():
        raise RunnerError('required TermsMet plugin is missing')
    scenario = Path(scenario or (HERE / SCENARIOS[case]))
    if not scenario.is_file():
        raise RunnerError('required TermsMet scenario is missing')
    cwd = Path(cwd).resolve()
    try:
        relative_scenario = str(scenario.resolve().relative_to(cwd))
    except ValueError as exc:
        raise RunnerError('scenario must be inside the Town checkout') from exc
    # Refuse to start a trial if the supervisor cannot inspect processes.
    _process_table()
    out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.termsmet-stage-', dir=out.parent) as stage_dir, \
            tempfile.TemporaryDirectory(prefix='termsmet-y4-') as temporary:
        pending = Path(stage_dir) / 'pending'
        pending.mkdir()
        temp = Path(temporary)
        env = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'TZ') if key in os.environ}
        env.update({'HOME': str(temp), 'NANDATOWN_HOME': str(temp / 'town-home'),
                    'TERMSMET_PROJECT_DIR': str(Path(project).resolve()),
                    'TERMSMET_PROJECT_PYTHON': os.path.abspath(project_python),
                    'TERMSMET_DB_PATH': str(temp / 'termsmet.sqlite'), 'TERMSMET_REVIEW_SECONDS': '2'})
        status = temp / 'status.json'
        config = {'case': case, 'plugin': str(Path(plugin).resolve()), 'scenario': relative_scenario,
                  'pending': str(pending), 'status': str(status)}
        command = [sys.executable, '-c',
                   'import importlib.util,json,sys; s=importlib.util.spec_from_file_location("tm_runner",sys.argv[1]); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); sys.exit(m._worker(json.loads(sys.argv[2])))',
                   str(Path(__file__).resolve()), json.dumps(config)]
        try:
            with tempfile.TemporaryFile(mode='w+') as log:
                worker = subprocess.Popen(command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                          stdout=log, stderr=log, start_new_session=True)
                code = _supervise(worker, timeout)
            if code != 0 or not status.is_file():
                info = json.loads(status.read_text()) if status.is_file() else {}
                raise RunnerError(info.get('status', 'run_failed'))
            info = json.loads(status.read_text())
            if info.get('status') != 'ok':
                raise RunnerError(info.get('status', 'run_failed'))
            name = info.get('bundle')
            if not isinstance(name, str) or Path(name).name != name:
                raise RunnerError('invalid bundle output')
            bundle = pending / name
            if not bundle.is_dir() or bundle.is_symlink():
                raise RunnerError('invalid bundle output')
            # Atomic directory promotion leaves Town's signed records untouched.
            os.replace(bundle, out)
        except BaseException as exc:
            if pending.exists() and any(pending.iterdir()):
                os.replace(pending, out / '.pending')
            (out / 'failure.json').write_text(json.dumps({'error': str(exc) if isinstance(exc, RunnerError) else 'run_failed'}) + '\n')
            if isinstance(exc, Exception) and not isinstance(exc, RunnerError):
                raise RunnerError('run_failed') from exc
            raise
    return out


def _validate_project(project, python):
    config = json.loads((HERE / 'project_version.json').read_text())
    project = Path(project).resolve()
    if not (project / 'server/mcp_server.py').is_file() or not os.path.isfile(python) or not os.access(python, os.X_OK):
        raise RunnerError('runtime project or interpreter is unavailable')
    try:
        revision = subprocess.run(['git', '-C', str(project), 'rev-parse', 'HEAD'], check=True, capture_output=True, text=True, timeout=5).stdout.strip()
        subprocess.run(['git', '-C', str(project), 'diff', '--quiet'], check=True, timeout=5)
        subprocess.run(['git', '-C', str(project), 'diff', '--cached', '--quiet'], check=True, timeout=5)
        version_info = subprocess.run([python, '-c', 'import importlib.metadata,platform; print(platform.python_version()); print(importlib.metadata.version("mcp"))'], check=True, capture_output=True, text=True, timeout=5).stdout.splitlines()
        town_mcp = importlib.metadata.version('mcp')
    except (OSError, subprocess.SubprocessError, importlib.metadata.PackageNotFoundError) as exc:
        raise RunnerError('cannot verify runtime revision, interpreter, or MCP SDK') from exc
    if (revision != config['project_commit'] or version_info != [config['project_python'], config['mcp_sdk']]
            or sys.version.split()[0] != config['town_python'] or town_mcp != config['mcp_sdk']):
        raise RunnerError('runtime or environment version differs from project_version.json')


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run or verify the TermsMet Town integration')
    parser.add_argument('--project')
    parser.add_argument('--project-python')
    parser.add_argument('--case', choices=tuple(SCENARIOS))
    parser.add_argument('--out')
    parser.add_argument('--verify')
    args = parser.parse_args(argv)
    try:
        if args.verify:
            if args.project or args.project_python or args.case or args.out:
                parser.error('--verify cannot be combined with live-run options')
            _verify(args.verify)
            print('Both bundles verified')
        else:
            if not args.project or not args.case or not args.out:
                parser.error('live run requires --project, --case and --out')
            python = args.project_python or str(Path(args.project) / '.venv/bin/python')
            # Missing formal files are reported before runtime setup.
            if not PLUGIN.is_file() or not (HERE / SCENARIOS[args.case]).is_file():
                raise RunnerError('formal TermsMet plugin or scenario is missing')
            _validate_project(args.project, python)
            _run_live(args.project, python, args.case, args.out)
            print('Run completed')
        return 0
    except RunnerError as exc:
        print(f'TermsMet runner: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
