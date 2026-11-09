"""Preserve release-section indentation through the full CLI and real Git tags."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

GITHUB = Path(__file__).resolve().parents[1]
SCRIPT = GITHUB / "scripts" / "generate_release_notes.py"
COMPONENTS = GITHUB / "components.json"


class ReleaseIndentCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = tempfile.TemporaryDirectory(prefix="release-indent-")
        self.addCleanup(self.workspace.cleanup)
        self.root = Path(self.workspace.name)
        scripts = self.root / ".github" / "scripts"
        scripts.mkdir(parents=True)
        self.script = scripts / SCRIPT.name
        self.metadata = scripts.parent / "components.json"
        shutil.copyfile(SCRIPT, self.script)
        shutil.copyfile(COMPONENTS, self.metadata)
        data = json.loads(self.metadata.read_text(encoding="utf-8"))
        self.prefix = "skill/v"
        self.component = next(
            c for c in data["components"] if c["release_tag_prefix"] == self.prefix
        )
        self.changelog = self.root / self.component["path_prefixes"][0] / "CHANGELOG.md"
        self.changelog.parent.mkdir(parents=True, exist_ok=True)
        self.notes = self.root / "release-notes.md"
        self.tag = self.prefix + "0.1.0"

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
        self.env.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_TERMINAL_PROMPT": "0",
                "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
        )
        self.git("init", "--quiet")
        self.git("config", "user.name", "Release Regression Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        self.git("config", "core.autocrlf", "false")
        self.git("config", "core.hooksPath", str(self.root / "no-hooks"))

    def git(self, *args: str) -> str:
        return subprocess.check_output(
            ["git", *args],
            cwd=self.root,
            env=self.env,
            encoding="utf-8",
            timeout=30,
        ).strip()

    def generate(self, body: str) -> tuple[subprocess.CompletedProcess[str], str, str]:
        self.changelog.write_text(
            "# Changelog\n\n## [0.1.0] - 2026-10-01\n\n"
            + body
            + "\n\n## [0.0.9] - 2026-09-01\n\n- Previous release only.\n",
            encoding="utf-8",
        )
        self.git("add", "--", str(self.changelog.relative_to(self.root)))
        self.git(
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--quiet",
            "-m",
            "test(ci): create private release fixture",
        )
        self.git("tag", self.tag)
        commit = self.git("rev-parse", "HEAD")
        # The generator must use the tagged version even if the working file
        # subsequently contains unrelated content.
        self.changelog.write_text("# Uncommitted working copy\n", encoding="utf-8")
        inputs = (self.changelog, self.script, self.metadata)
        before = tuple(path.read_bytes() for path in inputs)
        result = subprocess.run(
            [
                sys.executable,
                "-B",
                str(self.script),
                "--tag",
                self.tag,
                "--prefix",
                self.prefix,
                "--repository",
                "example/anolisa",
                "--output",
                str(self.notes),
            ],
            cwd=self.root,
            env=self.env,
            capture_output=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        self.assertEqual(tuple(path.read_bytes() for path in inputs), before)
        notes = self.notes.read_text(encoding="utf-8") if self.notes.exists() else ""
        return result, notes, commit

    def assert_notes_body(self, body: str) -> None:
        result, notes, _ = self.generate(body)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        expected = (
            f"## {self.component['display_name']} v0.1.0\n\n"
            + body
            + f"\n\n**Full Changelog**: https://github.com/example/anolisa/commits/{self.tag}\n"
        )
        self.assertEqual(notes, expected)
        self.assertNotIn("Previous release only", notes)
        self.assertNotIn("Uncommitted working copy", notes)

    def test_initial_four_space_code_indent_is_preserved(self) -> None:
        self.assert_notes_body(
            "    python -m migration --check\n"
            "    python -m migration --apply\n\n"
            "Run the check before applying the migration."
        )

    def test_initial_tab_code_indent_is_preserved(self) -> None:
        self.assert_notes_body("\tpython -m migration --check\n\tpython -m migration --apply")

    def test_plain_markdown_body_control(self) -> None:
        self.assert_notes_body("- Preserve ordinary notes.\n\n### Migration\n\nRun `check` first.")

    def test_fenced_markdown_body_control(self) -> None:
        self.assert_notes_body("```sh\npython -m migration --check\n```\n\n- Keep the explanation.")

    def test_empty_section_still_fails_without_writing_notes(self) -> None:
        result, notes, _ = self.generate(" \t\n")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Empty CHANGELOG section for 0.1.0", result.stderr)
        self.assertEqual(notes, "")
        self.assertFalse(self.notes.exists())


if __name__ == "__main__":
    unittest.main()
