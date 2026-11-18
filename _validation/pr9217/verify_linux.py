"""Fork-only #9217 Linux acceptance; invoked inside the capped service."""
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
TESTS = ROOT / 'src/os-skills/tests'
SCRIPT = ROOT / 'src/os-skills/ai/setup-mcp/scripts/validate_mcp.py'
FIX = (b'    # Preserve the host error policy while changing only the report encoding.\n'
       b'    if hasattr(sys.stdout, "reconfigure"):\n'
       b'        sys.stdout.reconfigure(encoding="utf-8", errors=sys.stdout.errors)\n')
OUTPUT = Path(os.environ['PR9217_EVIDENCE'])

def ids(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from ids(item)
        else:
            yield item.id()

def phase(name):
    suite = unittest.TestLoader().discover(str(TESTS))
    collected = list(ids(suite))
    stream = io.StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    data = {'collected': collected, 'tests_run': result.testsRun,
            'failures': [test.id() for test, _ in result.failures],
            'errors': [test.id() for test, _ in result.errors],
            'skipped': [{'id': test.id(), 'reason': why} for test, why in result.skipped],
            'expected_failures': [test.id() for test, _ in result.expectedFailures],
            'unexpected_successes': [test.id() for test in result.unexpectedSuccesses]}
    (OUTPUT / (name + '.json')).write_text(json.dumps(data, indent=2), encoding='utf-8')
    print(stream.getvalue(), end='')
    return 0 if result.wasSuccessful() else 1

def run_phase(name):
    result = subprocess.run([sys.executable, str(__file__), '--phase', name],
                            capture_output=True, text=True, encoding='utf-8', timeout=360)
    (OUTPUT / (name + '.log')).write_text(result.stdout + result.stderr, encoding='utf-8')
    print(result.stdout + result.stderr, end='', flush=True)
    data = json.loads((OUTPUT / (name + '.json')).read_text(encoding='utf-8'))
    data['returncode'] = result.returncode
    return data

def check_cli(ok):
    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / 'settings-\U0001f642.json'
        original = b'{"mcpServers":{"one":{"url":"https://example.invalid/mcp"}}}'
        config.write_bytes(original)
        result = subprocess.run([sys.executable, '-X', 'utf8=0', str(SCRIPT), '--check', str(config)],
            env=dict(os.environ, PYTHONIOENCODING='gbk:strict', PYTHONUTF8='0'),
            capture_output=True, timeout=30)
        assert config.read_bytes() == original
        if ok:
            assert result.returncode == 0 and result.stderr == b'', result
            assert result.stdout == f'OK: 1 server(s) in {config}\n'.encode('utf-8'), result
        else:
            assert result.returncode == 1 and b'UnicodeEncodeError' in result.stderr, result
        return {'returncode': result.returncode, 'stdout_hex': result.stdout.hex(),
                'stderr_hex': result.stderr.hex(), 'config_unchanged': True}

def capped_environment():
    assert sys.platform == 'linux' and sys.version_info[:2] == (3, 11), sys.version
    groups = Path('/proc/self/cgroup').read_text().splitlines()
    group = next(line.split(':', 2)[2] for line in groups if line.startswith('0::'))
    cgroup = Path('/sys/fs/cgroup') / group.lstrip('/')
    limits = {name: (cgroup / name).read_text().strip() for name in
              ('memory.max', 'memory.swap.max', 'pids.max')}
    assert limits == {'memory.max': '805306368', 'memory.swap.max': '0', 'pids.max': '16'}, limits
    affinity = sorted(os.sched_getaffinity(0))
    assert len(affinity) == 1, affinity
    assert Path(tempfile.gettempdir()).resolve() == Path(os.environ['TMPDIR']).resolve()
    return {'cgroup': str(cgroup), 'limits': limits, 'affinity': affinity,
            'private_tmp': tempfile.gettempdir(), 'runtime': sys.version}

def main():
    os.chdir(ROOT)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    environment = capped_environment()
    manifest = json.loads((HERE / 'inputs.json').read_text(encoding='utf-8'))
    for entry in manifest['formal_files']:
        raw = (ROOT / entry['path']).read_bytes()
        assert hashlib.sha256(raw).hexdigest() == entry['sha256'], entry['path']
    subprocess.run(['git', 'merge-base', '--is-ancestor', manifest['base'], 'HEAD'], check=True)
    subprocess.run(['git', 'diff', '--exit-code'], check=True)
    candidate = SCRIPT.read_bytes()
    assert candidate.count(FIX) == 1
    summary = {'head': subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip(),
               'base': manifest['base'], 'resources': environment}
    try:
        positive = run_phase('candidate')
        mcp = [name for name in positive['collected'] if name.startswith(('test_mcp_unicode_stdout.', 'test_mcp_stdout_error_policy.'))]
        assert sum(name.startswith('test_mcp_unicode_stdout.') for name in mcp) == 6
        assert sum(name.startswith('test_mcp_stdout_error_policy.') for name in mcp) == 7
        assert positive['returncode'] == 0 and not positive['failures'] and not positive['errors'], positive
        assert not any(item['id'] in mcp for item in positive['skipped']), positive['skipped']
        summary['candidate'] = positive
        summary['positive_cli'] = check_cli(True)
        SCRIPT.write_bytes(candidate.replace(FIX, b'', 1))
        negative = run_phase('without_stdout_fix')
        assert negative['collected'] == positive['collected']
        assert negative['returncode'] == 1 and len(negative['failures']) == 8, negative
        assert all(name in mcp for name in negative['failures']), negative
        assert not negative['errors'] and not negative['unexpected_successes'], negative
        summary['without_stdout_fix'] = negative
        summary['negative_cli'] = check_cli(False)
    finally:
        SCRIPT.write_bytes(candidate)
        summary['restored'] = SCRIPT.read_bytes() == candidate
        (OUTPUT / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    subprocess.run(['git', 'diff', '--exit-code'], check=True)
    assert not subprocess.check_output(['git', 'status', '--porcelain', '--untracked-files=all']).strip()
    summary['clean_checkout_after'] = True
    (OUTPUT / 'summary.json').write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(json.dumps(summary, indent=2), flush=True)

if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--phase':
        sys.exit(phase(sys.argv[2]))
    main()
