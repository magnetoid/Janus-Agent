"""Shared, stdlib-only helpers for importing another assistant's memory into Janus.

Lifted from the OpenClaw migration skill
(optional-skills/migration/openclaw-migration/scripts/openclaw_to_janus.py) so
core importers don't reach across the optional-skill boundary. Entries use the
same ``ENTRY_DELIMITER`` as tools/memory_tool.py, so a merged file round-trips
through the memory tool unchanged.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

ENTRY_DELIMITER = "\n§\n"   # must match tools/memory_tool.ENTRY_DELIMITER
DEFAULT_MEMORY_CHAR_LIMIT = 2200
DEFAULT_USER_CHAR_LIMIT = 1375

_SKIP_HEADINGS = re.compile(r"\b(MEMORY|USER|SOUL|AGENTS|TOOLS|IDENTITY|CLAUDE)\.md\b", re.I)


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip())


def parse_existing_memory_entries(path: Path) -> List[str]:
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8")
    if not raw.strip():
        return []
    if ENTRY_DELIMITER in raw:
        return [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]
    return extract_markdown_entries(raw)


def extract_markdown_entries(text: str) -> List[str]:
    """Turn a Markdown instructions/memory file into discrete, de-duplicated entries.

    Bullets become entries (prefixed with their heading path); paragraphs
    become one entry each; code blocks and tables are skipped."""
    entries: List[str] = []
    headings: List[str] = []
    paragraph_lines: List[str] = []

    def context_prefix() -> str:
        filtered = [h for h in headings if h and not _SKIP_HEADINGS.search(h)]
        return " > ".join(filtered)

    def flush_paragraph() -> None:
        nonlocal paragraph_lines
        if not paragraph_lines:
            return
        block = " ".join(line.strip() for line in paragraph_lines).strip()
        paragraph_lines = []
        if not block:
            return
        prefix = context_prefix()
        entries.append(f"{prefix}: {block}" if prefix else block)

    in_code = False
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()
        if stripped.startswith("```"):
            in_code = not in_code
            flush_paragraph()
            continue
        if in_code:
            continue
        m = re.match(r"^(#{1,6})\s+(.*\S)\s*$", stripped)
        if m:
            flush_paragraph()
            level = len(m.group(1))
            while len(headings) >= level:
                headings.pop()
            headings.append(m.group(2).strip())
            continue
        b = re.match(r"^\s*(?:[-*]|\d+\.)\s+(.*\S)\s*$", line)
        if b:
            flush_paragraph()
            prefix = context_prefix()
            content = b.group(1).strip()
            entries.append(f"{prefix}: {content}" if prefix else content)
            continue
        if not stripped:
            flush_paragraph()
            continue
        if stripped.startswith("|") and stripped.endswith("|"):
            flush_paragraph()
            continue
        paragraph_lines.append(stripped)
    flush_paragraph()

    deduped: List[str] = []
    seen = set()
    for e in entries:
        n = normalize_text(e)
        if not n or n in seen:
            continue
        seen.add(n)
        deduped.append(e.strip())
    return deduped


def merge_entries(
    existing: Sequence[str], incoming: Sequence[str], limit: int,
) -> Tuple[List[str], Dict[str, int], List[str]]:
    """Append new entries up to ``limit`` total chars; skips duplicates."""
    merged = list(existing)
    seen = {normalize_text(e) for e in existing if e.strip()}
    stats = {"existing": len(existing), "added": 0, "duplicates": 0, "overflowed": 0}
    overflowed: List[str] = []
    current_len = len(ENTRY_DELIMITER.join(merged)) if merged else 0
    for entry in incoming:
        n = normalize_text(entry)
        if not n:
            continue
        if n in seen:
            stats["duplicates"] += 1
            continue
        candidate_len = len(entry) if not merged else current_len + len(ENTRY_DELIMITER) + len(entry)
        if candidate_len > limit:
            stats["overflowed"] += 1
            overflowed.append(entry)
            continue
        merged.append(entry)
        seen.add(n)
        current_len = candidate_len
        stats["added"] += 1
    return merged, stats, overflowed


def write_entries_atomic(path: Path, entries: Sequence[str]) -> None:
    """Write entries in the memory tool's on-disk format via tmp+rename."""
    import os
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    content = ENTRY_DELIMITER.join(e.strip() for e in entries if e.strip())
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
