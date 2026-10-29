#!/usr/bin/env python3
"""Relative markdown link checker for the documentation tree.

Scans docs/**/*.md plus the root and component README files, extracts
relative links, and verifies each target exists in the repository.
External URLs, mailto and pure in-page anchors are skipped.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote, urlsplit

# Inline links and images: [text](target) / ![alt](target) — both follow the
# same relative path resolution rules, so a single pattern covers them.
LINK_RE = re.compile(
    r"""!?\[[^\]]*\]\((<[^>\n]*>|[^)\s]+)"""
    r"""(?:\s+(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\((?:\\.|[^()\\])*\)))?\)"""
)
SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def normalize_reference_label(label: str) -> str:
    return " ".join(label.split()).casefold()


def reference_prose(text: str, mask_inline: bool = True) -> str:
    """Mask code only for reference discovery; keep original character offsets."""
    lines = []
    fence = ""
    fence_size = 0
    fence_re = re.compile(r"^ {0,3}(" + chr(96) + r"{3,}|~{3,})(.*)$")
    for line in text.splitlines(keepends=True):
        content = line.rstrip("\r\n")
        # Recognize simple container fence openers without interpreting their
        # contents as reference uses. Complex container extent is conservative.
        content = re.sub(r"^(?: {0,3}>[ \t]?)+", "", content)
        content = re.sub(r"^ {0,3}(?:[-+*]|[0-9]{1,9}[.)])[ \t]{1,4}", "", content)
        match = fence_re.match(content)
        if fence:
            lines.append(re.sub(r"[^\r\n]", " ", line))
            if (
                match
                and match[1][0] == fence
                and len(match[1]) >= fence_size
                and not match[2].strip()
            ):
                fence = ""
            continue
        if match and (match[1][0] == "~" or chr(96) not in match[2]):
            fence, fence_size = match[1][0], len(match[1])
            lines.append(re.sub(r"[^\r\n]", " ", line))
        elif line.startswith(("    ", "\t")):
            lines.append(re.sub(r"[^\r\n]", " ", line))
        else:
            lines.append(line)
    prose = "".join(lines)
    masked = list(prose)
    cursor = 0
    while cursor < len(prose):
        if prose[cursor] == "\\":
            cursor += 2
            continue
        if prose.startswith("<!--", cursor):
            comment_end = prose.find("-->", cursor + 4)
            comment_end = len(prose) if comment_end < 0 else comment_end + 3
            for position in range(cursor, comment_end):
                if masked[position] not in "\r\n":
                    masked[position] = " "
            cursor = comment_end
            continue
        if prose[cursor] != chr(96):
            cursor += 1
            continue
        end = cursor + 1
        while end < len(prose) and prose[end] == chr(96):
            end += 1
        run = prose[cursor:end]
        closer = end
        while True:
            closer = prose.find(run, closer)
            if closer < 0 or re.search(r"\n[ \t]*\n", prose[end:closer]):
                cursor = end
                break
            if (closer == 0 or prose[closer - 1] != chr(96)) and (
                closer + len(run) == len(prose) or prose[closer + len(run)] != chr(96)
            ):
                if mask_inline:
                    for position in range(cursor, closer + len(run)):
                        if masked[position] not in "\r\n":
                            masked[position] = " "
                cursor = closer + len(run)
                break
            closer += len(run)
    return "".join(masked)


def reference_links(text: str) -> tuple[str, list[str]]:
    """Resolve used top-level definitions; leave unsupported definitions unvalidated."""
    prose = reference_prose(text, mask_inline=False)
    definition_start = re.compile(r"^ {0,3}\[([^\[\]\n]+)\]:[ \t]*(.*)$")
    definition_value = re.compile(
        r"""^(<[^>\n]*>|[^\s<>]+)"""
        r"""(?:[ \t]+(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|\((?:\\.|[^()\\])*\)))?[ \t]*$"""
    )
    definitions: dict[str, str | None] = {}
    uses = list(prose)
    inline_text = list(text)
    offset = 0
    lines = prose.splitlines(keepends=True)
    continuation_until = 0
    previous_definition = False
    for index, line in enumerate(lines):
        content = line.rstrip("\r\n")
        container = re.sub(r"^(?: {0,3}>[ \t]?)+", "", content)
        container = re.sub(r"^ {0,3}(?:[-+*]|[0-9]{1,9}[.)])[ \t]{1,4}", "", container)
        complex_definition = (
            definition_start.match(container) if container != content else None
        )
        if complex_definition:
            definitions.setdefault(
                normalize_reference_label(complex_definition[1]), None
            )
        block_start = index == 0 or not lines[index - 1].strip() or previous_definition
        match = (
            definition_start.match(content)
            if block_start and index >= continuation_until
            else None
        )
        previous_definition = match is not None
        if match:
            label = normalize_reference_label(match[1])
            value = definition_value.fullmatch(match[2])
            if index + 1 < len(lines) and lines[index + 1].lstrip().startswith(
                ('"', "'", "(")
            ):
                # Multiline title syntax is outside the bounded definition
                # reader. Ignore that definition and its continuation instead
                # of treating title examples or later duplicates as links.
                value = None
                continuation_until = index + 1
                while (
                    continuation_until < len(lines)
                    and lines[continuation_until].strip()
                ):
                    continuation_until += 1
            # Reserving unsupported first definitions prevents a later duplicate
            # from introducing a false missing-target report.
            if label and label not in definitions:
                definitions[label] = value[1] if value else None
        if match or complex_definition or index < continuation_until:
            for position in range(offset, offset + len(line)):
                if uses[position] not in "\r\n":
                    uses[position] = " "
                if inline_text[position] not in "\r\n":
                    inline_text[position] = " "
        offset += len(line)

    text_for_uses = reference_prose("".join(uses))
    targets: list[str] = []
    cursor = 0

    def close_bracket(start: int) -> int | None:
        depth = 1
        position = start + 1
        while position < len(text_for_uses):
            char = text_for_uses[position]
            if char == "\\":
                position += 2
                continue
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return position
            if char == "\n" and re.match(r"[ \t]*\n", text_for_uses[position + 1 :]):
                return None
            position += 1
        return None

    while cursor < len(text_for_uses):
        if text_for_uses[cursor] == "\\":
            cursor += 2
            continue
        if text_for_uses[cursor] != "[":
            cursor += 1
            continue
        end = close_bracket(cursor)
        if end is None:
            cursor += 1
            continue
        label = text_for_uses[cursor + 1 : end]
        following = end + 1
        if following < len(text_for_uses) and text_for_uses[following] == "(":
            # Inline destinations have precedence over shortcut references.
            # Consume their complete label/title, including nested brackets.
            inline = LINK_RE.match(text_for_uses, cursor)
            if inline:
                cursor = inline.end()
                continue
            depth = 1
            position = following + 1
            while position < len(text_for_uses) and depth:
                if text_for_uses[position] == "\\":
                    position += 2
                    continue
                if text_for_uses[position] == "(":
                    depth += 1
                elif text_for_uses[position] == ")":
                    depth -= 1
                position += 1
            cursor = position
            continue
        if following < len(text_for_uses) and text_for_uses[following] == "[":
            reference_end = close_bracket(following)
            if reference_end is None:
                cursor = following + 1
                continue
            explicit = text_for_uses[following + 1 : reference_end]
            label = explicit or label
            following = reference_end + 1
        target = definitions.get(normalize_reference_label(label))
        if target is not None:
            targets.append(target)
        cursor = following
    return "".join(inline_text), targets


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(out.stdout.strip())


def files_to_check(root: Path) -> list[Path]:
    # Scope: the published docs tree, root-level docs, and component README
    # entry points. Deep in-tree docs (design notes, SKILL.md assets) are
    # intentionally excluded to keep the gate focused on reader-facing paths.
    out = subprocess.run(
        [
            "git",
            "ls-files",
            "docs/**/*.md",
            ":(glob)docs/*.md",
            ":(glob)*.md",
            ":(glob)src/*/README*.md",
            ":(glob)src/*/python/*/README*.md",
        ],
        capture_output=True,
        text=True,
        check=True,
        cwd=root,
    )
    return [root / line for line in out.stdout.splitlines() if line]


def check_file(md: Path, root: Path) -> list[str]:
    errors = []
    text = md.read_text(encoding="utf-8", errors="replace")
    # Strip fenced code blocks so shell snippets are not parsed as links.
    text = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    text, references = reference_links(text)
    targets = [match.group(1) for match in LINK_RE.finditer(text)]
    for target in targets + references:
        destination = target[1:-1] if target.startswith("<") else target
        if SCHEME_RE.match(destination) or destination.startswith("//"):
            continue
        path_part = unquote(urlsplit(destination).path)
        if not path_part:
            continue
        resolved = (md.parent / path_part).resolve()
        if not resolved.exists():
            errors.append(f"{md.relative_to(root)}: broken link -> {target}")
    return errors


def main() -> int:
    root = repo_root()
    errors: list[str] = []
    for md in files_to_check(root):
        if not md.exists():  # deleted but still in index during rebase etc.
            continue
        errors.extend(check_file(md, root))
    if errors:
        print(f"✗ {len(errors)} broken relative link(s):")
        for err in errors:
            print(f"    {err}")
        return 1
    print("✓ All relative links resolve")
    return 0


if __name__ == "__main__":
    sys.exit(main())
