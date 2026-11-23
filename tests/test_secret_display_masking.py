"""Secrets shown in terminals and logs go through ``agent.redact.mask_secret``.

Hand-rolled slices such as ``f"{api_key[:12]}..."`` or ``f"...{key[-8:]}"``
showed up to 12 characters of a key, more than the canonical mask (4 + 4),
and nothing kept them consistent. Key formats without a fixed public prefix
(Azure, custom endpoints, app secrets) lost a real share of their entropy to
setup screens, the 401 diagnostic and the request debug dump, which users
paste into bug reports.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

from agent.redact import mask_secret

ROOT = Path(__file__).resolve().parents[1]
PRODUCTION = [
    "agent",
    "janus_cli",
    "gateway",
    "tools",
    "plugins",
    "cron",
    "acp_adapter",
    "tui_gateway",
    *[p.name for p in ROOT.glob("*.py")],
]

# Names that hold a secret. ``key`` alone is too common (dict keys, session
# keys) to flag everywhere, so it is only checked inside print/log calls.
_SECRET_NAME = re.compile(
    r"(api_?key|secret|password|passwd|token)$|^(existing|current|current_host|app)_key$",
    re.IGNORECASE,
)
_SINK_ONLY_NAME = re.compile(r"^key$")
_SLICE_HINT = re.compile(r"(key|secret|password|passwd|token)\s*\[\s*-?\d*\s*:", re.IGNORECASE)
_SINKS = {
    "print", "write", "debug", "info", "warning", "warn", "error", "exception",
    "critical", "log", "print_success", "print_info", "print_warning",
    "print_error", "masked_secret_prompt", "input",
}


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _secret_slices(node: ast.AST, matches) -> list[ast.Subscript]:
    found = []
    for sub in ast.walk(node):
        if isinstance(sub, ast.Subscript) and isinstance(sub.slice, ast.Slice):
            name = _name(sub.value)
            if name and matches(name):
                found.append(sub)
    return found


def _production_files():
    for entry in PRODUCTION:
        path = ROOT / entry
        if path.is_dir():
            yield from (p for p in path.rglob("*.py") if "tests" not in p.parts)
        elif path.is_file():
            yield path


def _offenders() -> list[str]:
    offenders = set()
    for path in _production_files():
        source = path.read_text(encoding="utf-8")
        # Cheap pre-filter so only files that slice a secret-ish name get parsed.
        if not _SLICE_HINT.search(source):
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            hits: list[ast.Subscript] = []
            if isinstance(node, ast.JoinedStr):
                for part in node.values:
                    if isinstance(part, ast.FormattedValue):
                        hits += _secret_slices(part.value, _SECRET_NAME.search)
            elif isinstance(node, ast.Call) and _name(node.func) in _SINKS:
                for arg in node.args:
                    hits += _secret_slices(
                        arg, lambda n: _SECRET_NAME.search(n) or _SINK_ONLY_NAME.search(n)
                    )
            for sub in hits:
                rel = path.relative_to(ROOT)
                offenders.add(f"{rel}:{sub.lineno}: {ast.unparse(sub)}")
    return sorted(offenders)


def test_no_hand_rolled_secret_slices_in_output():
    offenders = _offenders()
    assert not offenders, (
        "Show secrets with agent.redact.mask_secret(), not by slicing them:\n  "
        + "\n  ".join(offenders)
    )


def test_guard_flags_a_hand_rolled_slice(tmp_path, monkeypatch):
    """The scan above must actually catch the pattern it forbids."""
    sample = tmp_path / "sample.py"
    sample.write_text(
        'print(f"key: {api_key[:8]}...")\nlogger.info("tok %s", key[:12])\n',
        encoding="utf-8",
    )
    module = sys.modules[__name__]
    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setattr(module, "PRODUCTION", ["sample.py"])
    assert len(_offenders()) == 2


def test_request_dump_masks_keys_like_mask_secret():
    """The debug request dump (Authorization header) uses the canonical mask."""
    from run_agent import AIAgent

    key = "3f9c2a7e5b1d4c8a9e0f6b2d7a4c1e8f"  # Azure-style: no public prefix
    masked = AIAgent._mask_api_key_for_logs(object(), key)
    assert masked == mask_secret(key)
    assert key[4:-4] not in masked
