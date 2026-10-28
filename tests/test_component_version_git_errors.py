"""Complete version-gate CLI checks with actual metadata and private Git repositories."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-component-versions.py"


class VersionGitErrorsCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory(prefix="version-git-")
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name) / "checkout"
        (self.root / "scripts").mkdir(parents=True)
        self.script = self.root / "scripts" / SCRIPT.name
        shutil.copyfile(SCRIPT, self.script)

        # Import the complete module only to discover its current metadata-file
        # contract. No function, parser or ROOT is replaced; the tested execution
        # is always the complete copied CLI in a separate process.
        spec = importlib.util.spec_from_file_location("version_fixture_inputs", SCRIPT)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        paths = {path for pair in module.TOML_CONTRACTS for path in pair}
        paths.update(path for pair in module.VERSION_TEMPLATES for path in pair)
        paths.update(module.AGENT_MEMORY_JSON)
        paths.add("src/agent-memory/Cargo.toml")
        paths.add("src/agent-memory/adapters/agent-memory/openclaw/package-lock.json")
        self.generated = module.GENERATED_CONTRACTS[0]
        for name in paths:
            source = ROOT / name
            self.assertTrue(source.is_file(), f"complete official metadata required: {source}")
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)

        self.env = os.environ.copy()
        for name in (
            "GIT_DIR",
            "GIT_COMMON_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_CEILING_DIRECTORIES",
            "GIT_CONFIG_COUNT",
        ):
            self.env.pop(name, None)
        self.env["GIT_CONFIG_NOSYSTEM"] = "1"
        self.env["GIT_CONFIG_GLOBAL"] = os.devnull
        self.env["GIT_TERMINAL_PROMPT"] = "0"
        self.env["PYTHONUTF8"] = "1"
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.git("init", "--quiet")

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=check,
        )

    def track_generated(self) -> None:
        target = self.root / self.generated
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("# generated contract tracking fixture\n", encoding="utf-8")
        self.git("add", "--force", "--", self.generated)
        self.assertEqual(self.git("ls-files", "--error-unmatch", self.generated).returncode, 0)

    def run_gate(self) -> subprocess.CompletedProcess[str]:
        before = {
            p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()
        }
        result = subprocess.run(
            [sys.executable, str(self.script)],
            cwd=self.workspace.name,
            env=self.env,
            capture_output=True,
            encoding="utf-8",
            errors="strict",
            timeout=30,
            check=False,
        )
        after = {
            p.relative_to(self.root): p.read_bytes() for p in self.root.rglob("*") if p.is_file()
        }
        self.assertEqual(before, after, "the gate changed repository inputs")
        return result

    def assert_git_failure(self) -> None:
        probe = self.git("ls-files", "--error-unmatch", self.generated, check=False)
        self.assertNotIn(probe.returncode, (0, 1), probe.stderr)
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("git ls-files failed", result.stderr)
        self.assertIn(self.generated, result.stderr)
        self.assertIn(f"exit {probe.returncode}", result.stderr)
        self.assertNotIn("metadata is synchronized", result.stdout)

    def test_fatal_index_read_error_is_not_a_successful_untracked_check(self) -> None:
        self.track_generated()
        (self.root / ".git" / "index").write_bytes(b"invalid Git index fixture\n")
        self.assert_git_failure()

    def test_fatal_repository_config_read_error_is_not_success(self) -> None:
        self.track_generated()
        (self.root / ".git" / "config").write_text("[unterminated\n", encoding="utf-8")
        self.assert_git_failure()

    def test_real_tracked_generated_contract_is_still_rejected(self) -> None:
        self.track_generated()
        result = self.run_gate()
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("generated component contract must not be tracked", result.stderr)
        self.assertNotIn("git ls-files failed", result.stderr)

    def test_real_untracked_checkout_is_still_accepted(self) -> None:
        probe = self.git("ls-files", "--error-unmatch", self.generated, check=False)
        self.assertEqual(probe.returncode, 1, probe.stderr)
        result = self.run_gate()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Component version metadata is synchronized.", result.stdout)


if __name__ == "__main__":
    unittest.main()
