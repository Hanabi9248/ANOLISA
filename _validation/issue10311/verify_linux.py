"""Check the SkillFS bundled usage reports on the fork's Linux runner."""

import hashlib
import json
import locale
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[2]
OFFICIAL_BASE = "ebe18c4ca9f8e26187d0ace9be5c646dea2b9586"
LOCAL_CANDIDATE = "ee120e361c1e37eebae434beb7766bc06c4070af"
SCRIPTS = "src/skillfs/docs/skills/skillfs-mount/scripts"
TESTS = "src/skillfs/tests"
SESSION_SCRIPT = f"{SCRIPTS}/skill_usage_from_session_logs.py"
SESSION_TEST = f"{TESTS}/test_skill_usage_from_session_logs.py"
CHECKOUT_BLOBS = {
    SESSION_SCRIPT: "9dd9ae3961e9f1b8d5571d027e410769049afef0",
    SESSION_TEST: "d17fff444bc1bc7f81a49a9f21e151c07f41def8",
    f"{SCRIPTS}/skill_usage_from_chat_logs.py": "986e054197c400fa6c322ef5e7c8a9a0675c3216",
    f"{TESTS}/test_skill_usage_from_chat_logs.py": "58febb9950b4c7c6bf7c6760f04dab69e49776af",
    f"{TESTS}/test_skill_usage_chat_log_roots.py": "3c8017424622b1b855b3e1e06e58611453fb659b",
}
BLAS_THREADS = (
    "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS",
    "NUMEXPR_NUM_THREADS", "BLIS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS",
)


def git(*args: str) -> str:
    return subprocess.check_output(
        ["git", "-c", "core.preloadIndex=false", "-c", "index.threads=1", *args],
        cwd=ROOT, text=True,
    ).strip()


def verify_environment() -> None:
    assert sys.platform == "linux" and sys.version_info[:2] == (3, 11), sys.version
    membership = Path("/proc/self/cgroup").read_text().splitlines()
    group = next(line[3:] for line in membership if line.startswith("0::"))
    cgroup = Path("/sys/fs/cgroup") / group.lstrip("/")
    actual = {name: (cgroup / name).read_text().strip() for name in
              ("memory.max", "memory.swap.max", "pids.max")}
    assert actual == {"memory.max": "805306368", "memory.swap.max": "0", "pids.max": "16"}, actual
    affinity = sorted(os.sched_getaffinity(0))
    assert len(affinity) == 1, affinity
    private_tmp = Path(tempfile.gettempdir()).resolve()
    assert all(Path(os.environ[name]).resolve() == private_tmp for name in ("TMPDIR", "TMP", "TEMP"))
    assert private_tmp.stat().st_mode & 0o777 == 0o700, private_tmp
    assert all(os.environ.get(name) == "1" for name in BLAS_THREADS)
    print(f"Linux Python {sys.version.split()[0]}, locale {locale.getencoding()}, "
          f"CPU {affinity[0]}, cgroup limits {actual}, private temporary directory", flush=True)


def verify_source() -> None:
    assert git("rev-parse", "HEAD^") == OFFICIAL_BASE
    changed = set(git("diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD^", "HEAD").splitlines())
    assert changed == {
        SESSION_SCRIPT, SESSION_TEST,
        ".github/workflows/skillfs-linux-validation.yaml", "_validation/issue10311/verify_linux.py",
    }, changed
    for name, expected in CHECKOUT_BLOBS.items():
        raw = (ROOT / name).read_bytes()
        actual = hashlib.sha1(f"blob {len(raw)}\0".encode("ascii") + raw).hexdigest()
        assert actual == expected, (name, actual, expected)
    git("diff", "--exit-code", "HEAD")
    print(f"Fork checkout {git('rev-parse', 'HEAD')}, official parent {OFFICIAL_BASE}; "
          f"five verified blobs match local candidate {LOCAL_CANDIDATE}", flush=True)


def verify_report(logs: Path, output: Path, workspace: str) -> None:
    proc = subprocess.run(
        [sys.executable, str(ROOT / SESSION_SCRIPT), "--logs-dir", str(logs), "--output", str(output)],
        capture_output=True, timeout=60,
    )
    assert proc.returncode == 0 and not proc.stderr, proc
    assert b"Warning: Could not read config" not in proc.stdout, proc.stdout
    raw = output.read_bytes()
    assert workspace.encode("utf-8") in raw, raw
    report = json.loads(raw.decode("utf-8"))
    assert report["workspaces"] == {"agent-1": workspace}, report


def verify_cli() -> None:
    with tempfile.TemporaryDirectory(prefix="skillfs-usage-") as directory:
        logs = Path(directory)
        sessions = logs / "agents" / "agent-1" / "sessions"
        sessions.mkdir(parents=True)
        session_log = sessions / "s1.jsonl"
        session_log.write_text("{}\n", encoding="utf-8")
        config_path = logs / "openclaw.json"
        config_workspace = "/fixtures/workspace-\u20ac"
        config = {"agents": {"list": [{"id": "agent-1", "workspace": config_workspace}]}}
        config_bytes = json.dumps(config, ensure_ascii=False).encode("utf-8")
        config_path.write_bytes(config_bytes)
        verify_report(logs, logs / "config-report.json", config_workspace)
        assert config_path.read_bytes() == config_bytes

        config_path.unlink()
        session_workspace = "/fixtures/workspace-\U0001f9ea"
        session_log.write_text(
            json.dumps({"cwd": session_workspace}, ensure_ascii=False) + "\n", encoding="utf-8",
        )
        verify_report(logs, logs / "session-report.json", session_workspace)
    print("Both CLI reports contain complete UTF-8 JSON and literal Unicode workspaces.", flush=True)


def main() -> None:
    verify_environment()
    verify_source()
    subprocess.run(
        ["python3", "-m", "unittest", "discover", "-s", TESTS, "-p", "test_skill_usage*.py"],
        cwd=ROOT, check=True, timeout=360,
    )
    verify_cli()
    git("diff", "--exit-code", "HEAD")


if __name__ == "__main__":
    main()
