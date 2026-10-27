"""Import another assistant's setup into Janus: Claude Code, Codex, Cursor.

Lowers switching cost the way OpenClaw's memory import and Hermes's
``import-agent`` do, but with Janus's trust posture:

  * **Credentials are never read.** ``~/.claude/.credentials.json`` and
    ``~/.codex/auth.json`` are only ever checked for existence (to print an
    advisory). Janus already gates external tool creds deliberately.
  * **Skills are scanned before they touch the live tree.** Each imported skill
    is staged into a temp dir and run through the same scanner hub installs use;
    a scanner failure blocks the skill (fail closed — skills are code).
  * **Permission allow-lists are advisory only.** Importing Claude Code's
    ``permissions.allow`` Bash patterns would weaken Janus's approvals.
  * **Secrets in MCP env are skipped** unless ``--migrate-secrets``.
  * Non-destructive on the source; idempotent on the destination (duplicate
    memory entries are detected, existing skills/servers are left alone unless
    ``--overwrite``).

Follows the ``hermes_migrate`` shape: pure ``detect``/``plan``/``migrate``
functions, a thin ``import_command`` CLI handler with lazy interactive imports.
``Path.home()`` here refers to the *other* assistant's home — not Janus's —
which is why it is used directly instead of ``get_janus_home()``.
"""
from __future__ import annotations

import json
import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from janus_cli.import_common import (
    DEFAULT_MEMORY_CHAR_LIMIT,
    extract_markdown_entries,
    merge_entries,
    normalize_text,
    parse_existing_memory_entries,
    write_entries_atomic,
)

logger = logging.getLogger(__name__)

SOURCES: Tuple[str, ...] = ("claude-code", "codex", "cursor")
_DEFAULT_ROOTS = {"claude-code": ".claude", "codex": ".codex", "cursor": ".cursor"}
_SECRET_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")
_MCP_FIELDS = ("command", "args", "env", "url", "headers", "type")


@dataclass
class ImportPlan:
    source: str
    root: Path
    memory_entries: List[str] = field(default_factory=list)
    skills: List[Path] = field(default_factory=list)      # skill dirs, or command .md files
    mcp_servers: Dict[str, dict] = field(default_factory=dict)
    advisories: List[str] = field(default_factory=list)   # deliberately NOT imported
    notes: List[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.memory_entries or self.skills or self.mcp_servers)


# ── detection / planning ─────────────────────────────────────────────────


def detect(source: str, override: Optional[Path] = None) -> Optional[Path]:
    """The assistant's home dir if present (``override`` wins), else None."""
    if source not in SOURCES:
        return None
    root = Path(override).expanduser() if override else Path.home() / _DEFAULT_ROOTS[source]
    return root if root.is_dir() else None


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


def _mcp_from_mapping(servers: Any) -> Dict[str, dict]:
    out: Dict[str, dict] = {}
    if not isinstance(servers, dict):
        return out
    for name, sc in servers.items():
        if not isinstance(sc, dict):
            continue
        conv = {k: sc[k] for k in _MCP_FIELDS if k in sc}
        if conv.get("command") or conv.get("url"):
            out[str(name)] = conv
    return out


def _mcp_from_json(path: Path, key: str = "mcpServers") -> Dict[str, dict]:
    try:
        data = json.loads(_read(path) or "{}")
    except Exception:
        return {}
    return _mcp_from_mapping(data.get(key)) if isinstance(data, dict) else {}


def _mcp_from_toml(path: Path, key: str = "mcp_servers") -> Dict[str, dict]:
    try:
        import tomllib
        data = tomllib.loads(_read(path) or "")
    except Exception:
        return {}
    return _mcp_from_mapping(data.get(key))


def _dedupe(entries: List[str]) -> List[str]:
    seen, out = set(), []
    for e in entries:
        n = normalize_text(e)
        if n and n not in seen:
            seen.add(n)
            out.append(e)
    return out


def plan(source: str, root: Path) -> ImportPlan:
    """Inventory what can be imported. Reads instructions/memory/skills/MCP only."""
    p = ImportPlan(source=source, root=root)
    if source == "claude-code":
        for f in [root / "CLAUDE.md", *sorted(root.glob("projects/*/memory/MEMORY.md"))]:
            if f.is_file():
                p.memory_entries += extract_markdown_entries(_read(f))
        skills_dir = root / "skills"
        if skills_dir.is_dir():
            p.skills += [d for d in sorted(skills_dir.iterdir()) if (d / "SKILL.md").is_file()]
        cmds = root / "commands"
        if cmds.is_dir():
            p.skills += sorted(cmds.glob("*.md"))
        for cand in (root.parent / ".claude.json", Path.home() / ".claude.json"):
            if cand.is_file():
                p.mcp_servers = _mcp_from_json(cand)
                break
        settings = root / "settings.json"
        if settings.is_file():
            try:
                allow = ((json.loads(_read(settings)) or {}).get("permissions") or {}).get("allow") or []
            except Exception:
                allow = []
            if allow:
                p.advisories.append(
                    f"Not importing {len(allow)} Claude Code permissions.allow pattern(s) — "
                    "pre-approving commands would weaken Janus approvals. Review them with "
                    "`janus config` → command_allowlist if you really want them.")
        if (root / ".credentials.json").exists():   # existence only — never read
            p.advisories.append("Credentials are never imported; configure providers with `janus setup`.")
    elif source == "codex":
        if (root / "AGENTS.md").is_file():
            p.memory_entries += extract_markdown_entries(_read(root / "AGENTS.md"))
        skills_dir = root / "skills"
        if skills_dir.is_dir():
            p.skills += [d for d in sorted(skills_dir.iterdir()) if (d / "SKILL.md").is_file()]
        cfg = root / "config.toml"
        if cfg.is_file():
            p.mcp_servers = _mcp_from_toml(cfg)
            try:
                import tomllib
                data = tomllib.loads(_read(cfg) or "")
                model = data.get("model"); prov = data.get("model_provider")
                if model or prov:
                    p.notes.append(
                        f"Codex model setting ({prov or '?'} / {model or '?'}) is not imported — "
                        "model IDs don't map 1:1; pick a provider with `janus model`.")
            except Exception:
                pass
        if (root / "auth.json").exists():   # existence only — never read
            p.advisories.append("Codex auth.json is never imported; configure providers with `janus setup`.")
    elif source == "cursor":
        mcp = root / "mcp.json"
        if mcp.is_file():
            p.mcp_servers = _mcp_from_json(mcp)
        p.notes.append("Cursor project rules (.cursor/rules, .cursorrules) are already read from the "
                       "working directory at runtime — nothing to import.")
    p.memory_entries = _dedupe(p.memory_entries)
    return p


# ── applying ─────────────────────────────────────────────────────────────


def _looks_secret(name: str) -> bool:
    up = str(name).upper()
    return any(h in up for h in _SECRET_HINTS)


def _memory_limit_from_config() -> int:
    try:
        from janus_cli.config import load_config
        v = (load_config().get("memory") or {}).get("memory_char_limit")
        return int(v) if v else DEFAULT_MEMORY_CHAR_LIMIT
    except Exception:
        return DEFAULT_MEMORY_CHAR_LIMIT


def _wrap_command_as_skill(name: str, body: str) -> str:
    desc = ""
    for line in body.splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            desc = s[:60]
            break
    return (f"---\nname: {name}\ndescription: {desc or ('Imported command ' + name)}\n"
            f"metadata:\n  janus:\n    category: imported\n---\n\n{body.strip()}\n")


def _scan_ok(stage: Path) -> Tuple[bool, str]:
    """Run the skills scanner on a staged copy. Fails CLOSED — skills are code."""
    try:
        from tools.skills_guard import scan_skill, should_allow_install
        return should_allow_install(scan_skill(stage, source="community"))
    except Exception as exc:
        return False, f"scanner unavailable: {exc}"


def migrate(
    plan_: ImportPlan, janus_home: Path, *, apply: bool,
    overwrite: bool = False, migrate_secrets: bool = False,
    memory_limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Apply (or preview, ``apply=False``) an ImportPlan into ``janus_home``."""
    res: Dict[str, Any] = {
        "memory_added": 0, "memory_duplicates": 0, "memory_overflowed": 0,
        "skills": [], "skills_blocked": [], "mcp_added": [], "mcp_skipped": [],
        "advisories": list(plan_.advisories), "notes": list(plan_.notes),
    }

    # Memory → memories/MEMORY.md (entry-merged, de-duplicated, char-capped)
    mem_path = janus_home / "memories" / "MEMORY.md"
    existing = parse_existing_memory_entries(mem_path)
    merged, stats, _overflow = merge_entries(
        existing, plan_.memory_entries, memory_limit or _memory_limit_from_config())
    res["memory_added"] = stats["added"]
    res["memory_duplicates"] = stats["duplicates"]
    res["memory_overflowed"] = stats["overflowed"]
    if apply and stats["added"]:
        write_entries_atomic(mem_path, merged)

    # Skills → skills/imported/<name>/ (staged + scanned first)
    dst_root = janus_home / "skills" / "imported"
    for item in plan_.skills:
        name = item.name if item.is_dir() else item.stem
        target = dst_root / name
        if target.exists() and not overwrite:
            res["skills_blocked"].append({"name": name, "reason": "already exists (use --overwrite)"})
            continue
        if not apply:
            res["skills"].append(name)
            continue
        with tempfile.TemporaryDirectory(prefix="janus-import-") as td:
            stage = Path(td) / name
            if item.is_dir():
                shutil.copytree(item, stage)
            else:
                stage.mkdir(parents=True)
                (stage / "SKILL.md").write_text(_wrap_command_as_skill(name, _read(item)), encoding="utf-8")
            ok, reason = _scan_ok(stage)
            if not ok:
                res["skills_blocked"].append({"name": name, "reason": f"blocked by scanner: {reason}"})
                continue
            dst_root.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target)
            shutil.copytree(stage, target)
        res["skills"].append(name)

    # MCP servers → config.yaml mcp_servers
    try:
        from janus_cli.mcp_config import _get_mcp_servers, _save_mcp_server
        existing_mcp = _get_mcp_servers() or {}
    except Exception:
        _save_mcp_server = None  # type: ignore[assignment]
        existing_mcp = {}
    for name, sc in plan_.mcp_servers.items():
        if name in existing_mcp and not overwrite:
            res["mcp_skipped"].append({"name": name, "reason": "already exists (use --overwrite)"})
            continue
        env = sc.get("env") or {}
        if isinstance(env, dict) and env and not migrate_secrets and any(_looks_secret(k) for k in env):
            res["mcp_skipped"].append({"name": name, "reason": "env holds secrets (use --migrate-secrets)"})
            continue
        if apply:
            if _save_mcp_server is None:
                res["mcp_skipped"].append({"name": name, "reason": "mcp config unavailable"})
                continue
            _save_mcp_server(name, dict(sc))
        res["mcp_added"].append(name)
    return res


# ── CLI ──────────────────────────────────────────────────────────────────


def import_command(args) -> int:
    """Handler for ``janus migrate claude-code|codex|cursor``. Returns an exit code."""
    from janus_cli.config import get_janus_home
    from janus_cli.setup import print_header, print_info, print_success, print_warning, prompt_yes_no

    source = str(getattr(args, "migrate_type", "") or "")
    if source not in SOURCES:
        print_warning(f"Unknown import source: {source!r} (choose from: {', '.join(SOURCES)})")
        return 2
    raw_src = getattr(args, "source", None)
    root = detect(source, Path(raw_src) if raw_src else None)
    if not root:
        print_warning(f"No {source} install found (looked for ~/{_DEFAULT_ROOTS[source]}).")
        print_info("Pass --source PATH if it lives elsewhere.")
        return 1

    pl = plan(source, root)
    janus_home = get_janus_home()
    preview = migrate(pl, janus_home, apply=False,
                      overwrite=bool(getattr(args, "overwrite", False)),
                      migrate_secrets=bool(getattr(args, "migrate_secrets", False)))
    print_header(f"Import preview — {source}")
    print_info(f"From: {root}")
    print_info(f"To:   {janus_home}")
    print_info(f"Memory entries: +{preview['memory_added']} "
               f"({preview['memory_duplicates']} duplicates, {preview['memory_overflowed']} over the cap)")
    print_info(f"Skills: {len(preview['skills'])} to import"
               + (f", {len(preview['skills_blocked'])} skipped" if preview["skills_blocked"] else ""))
    print_info(f"MCP servers: {len(preview['mcp_added'])} to import"
               + (f", {len(preview['mcp_skipped'])} skipped" if preview["mcp_skipped"] else ""))
    for s in preview["skills_blocked"] + preview["mcp_skipped"]:
        print_info(f"  · {s['name']}: {s['reason']}")
    for note in preview["advisories"] + preview["notes"]:
        print_info(f"  ! {note}")
    if not (preview["memory_added"] or preview["skills"] or preview["mcp_added"]):
        print_info("Nothing to import.")
        return 0
    if getattr(args, "dry_run", False):
        print_info("Dry run — no changes made. Re-run without --dry-run to apply.")
        return 0
    if not getattr(args, "yes", False) and not prompt_yes_no("Proceed with import?", default=False):
        print_info("Import cancelled.")
        return 1

    res = migrate(pl, janus_home, apply=True,
                  overwrite=bool(getattr(args, "overwrite", False)),
                  migrate_secrets=bool(getattr(args, "migrate_secrets", False)))
    print_success(f"Imported +{res['memory_added']} memory entries, {len(res['skills'])} skills, "
                  f"{len(res['mcp_added'])} MCP servers.")
    for s in res["skills_blocked"]:
        print_warning(f"skill {s['name']}: {s['reason']}")
    return 0
