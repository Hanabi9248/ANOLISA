"""Exercise the existing PR9760 regression and its standalone Make entry."""

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path


CANDIDATE = "3af2dc671b5edd6a27d78056f308ecea7c2d6b49"
BASELINE = "5a113912348aa2484c793cf1a52bd31f69d0c3b2"
BASELINE_BLOB = "e74030e37236aed1c905c411b18fd4fbd13b586b"
COMPONENT = Path("src/tokenless")
SOURCE = "adapters/tokenless/common/tokenless-env-fix.sh"
TEST = "tests/test_env_fix_normalization.py"
BLOBS = {
    SOURCE: "f5d17d9168204ea1e16d7f2c7a66d66151f8c395",
    TEST: "2cf93846a16ca0a26d1f24d6a00b31af8e05852b",
    "Makefile": "ecdacd1861cbc4b8bc69106791cac53deb8d0ef9",
}


def blob(path):
    data = path.read_bytes()
    return hashlib.sha1(f"blob {len(data)}\0".encode() + data).hexdigest()


def run_logged(output, name, command, extra_env=None):
    env = os.environ.copy()
    for key in ("ENV_FIX_TEST_SOURCE", "ENV_FIX_TEST_BASH", "ENV_FIX_TEST_JQ"):
        env.pop(key, None)
    env.update(extra_env or {})
    result = subprocess.run(
        command,
        cwd=COMPONENT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=120,
        check=False,
    )
    (output / f"{name}.log").write_bytes(result.stdout)
    text = result.stdout.decode("utf-8", errors="replace")
    print(text, end="", flush=True)
    return result.returncode, text


def main():
    evidence = Path(os.environ["PR9760_EVIDENCE"])
    output = evidence / "results"
    baseline = evidence / "tmp" / "baseline.sh"
    cgroup_line = next(
        line for line in Path("/proc/self/cgroup").read_text().splitlines()
        if line.startswith("0::")
    )
    cgroup = Path("/sys/fs/cgroup") / cgroup_line[3:].lstrip("/")
    limits = {
        name: (cgroup / name).read_text().strip()
        for name in ("memory.max", "memory.swap.max", "pids.max")
    }
    assert limits == {"memory.max": "805306368", "memory.swap.max": "0", "pids.max": "16"}, limits
    cpu = sorted(os.sched_getaffinity(0))
    assert len(cpu) == 1, cpu
    assert platform.system() == "Linux" and sys.version_info[:2] == (3, 11)
    assert os.getuid() != 0
    assert any(line.split() == ["NoNewPrivs:", "1"] for line in Path("/proc/self/status").read_text().splitlines())
    for path, expected in BLOBS.items():
        assert blob(COMPONENT / path) == expected, path
    assert blob(baseline) == BASELINE_BLOB
    tools = {}
    for name in ("bash", "jq", "make", "python3"):
        path = shutil.which(name)
        assert path, f"Missing required Linux executable: {name}"
        tools[name] = str(Path(path).resolve())
    assert Path(tools["python3"]).samefile(sys.executable)
    versions = {
        name: subprocess.check_output([path, "--version"], text=True).splitlines()[0]
        for name, path in tools.items()
    }
    summary = {
        "workflow_sha": os.environ["PR9760_WORKFLOW_SHA"],
        "candidate": CANDIDATE,
        "baseline": BASELINE,
        "baseline_blob": BASELINE_BLOB,
        "candidate_blobs": BLOBS,
        "kernel": platform.release(),
        "tool_versions": versions,
        "limits": limits,
        "cpu_affinity": cpu,
        "full_component_build_lint_test": "NOT_RUN",
        "cargo_and_full_python_integration": "NOT_RUN",
        "documentation_website_build": "NOT_RUN",
        "installation_paths": "NOT_RUN",
    }
    try:
        code, _ = run_logged(output, "candidate-shell-syntax", [tools["bash"], "-n", SOURCE])
        assert code == 0
        code, text = run_logged(
            output,
            "baseline-read-only",
            [sys.executable, TEST, "EnvFixNormalizationTests.test_check_lists_binary_and_package_without_constraint"],
            {"ENV_FIX_TEST_SOURCE": str(baseline), "ENV_FIX_TEST_JQ": tools["jq"]},
        )
        assert code == 1 and "FAILED (failures=1)" in text
        assert "AssertionError: False is not true" in text and "jq>=1.0.0 — " in text
        summary["baseline_read_only"] = "EXPECTED_ASSERTION_FAILURE"
        code, text = run_logged(output, "candidate-make", [tools["make"], "-j1", "test-env-fix-normalization"])
        assert code == 0 and "Ran 9 tests in " in text and "\nOK\n" in text
        summary["standalone_make_regressions"] = {"passed": 9, "skipped": 0}
    finally:
        for filename in ("memory.peak", "memory.events", "pids.peak", "pids.events"):
            path = cgroup / filename
            if path.exists():
                summary[filename] = path.read_text().strip()
        serialized = json.dumps(summary, indent=2) + "\n"
        (output / "summary.json").write_text(serialized, encoding="utf-8")
        print(serialized, end="", flush=True)


if __name__ == "__main__":
    main()
