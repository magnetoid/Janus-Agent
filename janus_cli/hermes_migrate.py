"""Migrate a legacy ``~/.hermes`` install into Janus (~/.janus).

A legacy ``~/.hermes`` home has an on-disk layout IDENTICAL to Janus
(config.yaml, .env, memories/, skills/, sessions/, learning/, state DB, …), so
this is a safe directory COPY — not a format translation — plus a targeted
rewrite of two tokens inside config.yaml/.env: ``HERMES_HOME`` ->
``JANUS_HOME`` and filesystem paths ``.hermes`` -> ``.janus``.

The rewrite is anchored to ``HERMES_HOME`` and the dot-prefixed ``/.hermes``
path, so other strings that merely contain "hermes" (for example model IDs,
which have no leading dot and no HOME suffix) are never hit.

Non-destructive: copies into the Janus home and leaves ~/.hermes intact; the
caller may offer to delete it afterward. Best-effort; never raises.
"""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# files whose CONTENTS get the brand-token rewrite (paths/env vars only)
_REWRITE_FILES = ("config.yaml", ".env")
# markers that a directory really is a legacy ~/.hermes or Janus home (avoid migrating junk)
_HOME_MARKERS = ("config.yaml", ".env", "memories", "skills", "sessions", "state.db")


def default_legacy_dir() -> Optional[Path]:
    r"""The legacy home: ``$HERMES_HOME``, else ``~/.hermes`` (or
    ``%LOCALAPPDATA%\hermes`` on Windows)."""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env).expanduser()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA")
        if base:
            return Path(base) / "hermes"
    return Path.home() / ".hermes"


def looks_like_home(path: Path) -> bool:
    """True if ``path`` is a directory containing at least one home marker."""
    return path.is_dir() and any((path / m).exists() for m in _HOME_MARKERS)


def legacy_hermes_home() -> Optional[Path]:
    """Return the legacy ~/.hermes home if it exists and looks real, else None."""
    d = default_legacy_dir()
    if d and looks_like_home(d):
        return d
    return None


def _rewrite_brand_tokens(text: str) -> str:
    """Targeted, SAFE rewrite: only the home env var + the .hermes config path.

    Anchored so it never matches other "hermes" substrings such as model IDs
    (no leading dot, no HOME suffix).
    """
    text = text.replace("HERMES_HOME", "JANUS_HOME")
    text = text.replace("/.hermes", "/.janus")    # POSIX config-dir paths
    text = text.replace("\\.hermes", "\\.janus")  # Windows config-dir paths
    return text


def plan_migration(src: Path, dst: Path) -> Dict[str, Any]:
    """Dry-run preview: classify each top-level entry in ``src`` as import or conflict.

    Directories are always ``import`` because they're *merged* (new files copied
    in, existing ones kept) — a pre-existing empty ``memories/`` must not block
    the real memory files. Only a singular top-level FILE that already exists in
    ``dst`` is a ``conflict`` (skipped unless ``--overwrite``).
    """
    result: Dict[str, Any] = {"src": str(src), "dst": str(dst), "import": [], "conflict": []}
    if not src.is_dir():
        return result
    for entry in sorted(src.iterdir(), key=lambda p: p.name):
        target = dst / entry.name
        if entry.is_dir():
            result["import"].append(entry.name)
        elif target.exists():
            result["conflict"].append(entry.name)
        else:
            result["import"].append(entry.name)
    return result


def _merge_dir(src_dir: Path, dst_dir: Path, overwrite: bool) -> bool:
    """Recursively copy ``src_dir`` into ``dst_dir``; returns True if anything copied.

    Files that already exist in ``dst_dir`` are kept unless ``overwrite``.
    """
    copied = False
    for root, _dirs, files in os.walk(src_dir):
        rel = Path(root).relative_to(src_dir)
        target_root = dst_dir / rel
        if not target_root.exists():
            target_root.mkdir(parents=True, exist_ok=True)
            copied = True
        for fname in files:
            target = target_root / fname
            if target.exists() and not overwrite:
                continue
            shutil.copy2(Path(root) / fname, target)
            copied = True
    return copied


def migrate(src: Path, dst: Path, *, overwrite: bool = False, apply: bool = True) -> Dict[str, Any]:
    """Copy top-level entries from ``src`` into ``dst`` (non-destructive on src).

    Conflicts (entries already in ``dst``) are skipped unless ``overwrite``.
    After copying, brand tokens in ``dst/config.yaml`` and ``dst/.env`` are
    rewritten. ``apply=False`` is a dry run that creates nothing. Best-effort;
    never raises. Returns a summary dict.
    """
    result: Dict[str, Any] = {
        "imported": [], "skipped": [], "errors": [], "rewritten": [], "applied": apply,
    }
    try:
        if not src.is_dir():
            result["errors"].append(f"source not found: {src}")
            return result
        if apply:
            dst.mkdir(parents=True, exist_ok=True)
        for entry in sorted(src.iterdir(), key=lambda p: p.name):
            target = dst / entry.name
            # A singular FILE that already exists is a conflict (skip unless
            # overwrite); DIRECTORIES are merged so a pre-existing empty dir
            # never blocks the real data inside.
            if entry.is_file() and target.exists() and not overwrite:
                result["skipped"].append(entry.name)
                continue
            if not apply:
                result["imported"].append(entry.name)
                continue
            try:
                if entry.is_dir():
                    if target.exists() and not target.is_dir():
                        if overwrite:
                            target.unlink()
                        else:
                            result["skipped"].append(entry.name)
                            continue
                    if _merge_dir(entry, target, overwrite):
                        result["imported"].append(entry.name)
                    else:
                        result["skipped"].append(entry.name)
                else:
                    shutil.copy2(entry, target)
                    result["imported"].append(entry.name)
            except Exception as exc:
                result["errors"].append(f"{entry.name}: {exc}")
        if apply:
            for name in _REWRITE_FILES:
                f = dst / name
                if f.is_file():
                    try:
                        original = f.read_text(encoding="utf-8")
                        rewritten = _rewrite_brand_tokens(original)
                        if rewritten != original:
                            f.write_text(rewritten, encoding="utf-8")
                            result["rewritten"].append(name)
                    except Exception as exc:
                        result["errors"].append(f"rewrite {name}: {exc}")
    except Exception as exc:
        logger.debug("hermes migrate failed: %s", exc)
        result["errors"].append(str(exc))
    return result


def remove_legacy(src: Path) -> bool:
    """Delete the legacy ~/.hermes home after a successful migration. Best-effort."""
    try:
        shutil.rmtree(src)
        return True
    except Exception as exc:
        logger.debug("remove legacy hermes home failed: %s", exc)
        return False


def migrate_command(args) -> int:
    """CLI handler for ``janus migrate hermes``. Returns a process exit code.

    Preview -> confirm -> copy -> (offer to delete the legacy home). Interactive
    helpers are imported lazily so the core functions above stay dependency-free.
    """
    from janus_cli.setup import (
        print_header, print_info, print_success, print_warning, prompt_yes_no,
    )
    from janus_cli.config import get_janus_home

    raw_source = getattr(args, "source", None)
    src = Path(raw_source).expanduser() if raw_source else legacy_hermes_home()
    if not src or not src.is_dir():
        print_warning("No previous install found (looked for ~/.hermes or $HERMES_HOME).")
        print_info("Pass --source PATH if your data lives elsewhere.")
        return 1

    dst = get_janus_home()
    overwrite = bool(getattr(args, "overwrite", False))
    yes = bool(getattr(args, "yes", False))

    plan = plan_migration(src, dst)
    print_header("Migration preview")
    print_info(f"From: {src}")
    print_info(f"To:   {dst}")
    if plan["import"]:
        print_info("Will import: " + ", ".join(plan["import"]))
    if plan["conflict"]:
        how = "OVERWRITE" if overwrite else "skip (already in Janus)"
        print_info(f"Already present ({how}): " + ", ".join(plan["conflict"]))
    will_change = plan["import"] or (overwrite and plan["conflict"])
    if not will_change:
        print_info("Nothing to import — Janus already has this data.")
        return 0
    if getattr(args, "dry_run", False):
        print_info("Dry run — no changes made. Re-run without --dry-run to apply.")
        return 0
    if not yes and not prompt_yes_no("Proceed with migration?", default=False):
        print_info("Migration cancelled.")
        return 1

    res = migrate(src, dst, overwrite=overwrite, apply=True)
    if res["imported"]:
        print_success(f"Imported {len(res['imported'])} item(s).")
    if res["rewritten"]:
        print_info("Rewrote home paths/env in: " + ", ".join(res["rewritten"]))
    if res["skipped"]:
        print_info(f"Left {len(res['skipped'])} existing item(s) untouched (use --overwrite to force).")
    if res["errors"]:
        print_warning(f"{len(res['errors'])} error(s): " + "; ".join(res["errors"][:3]))
        return 1

    # Copy-then-offer-cleanup: the original is left in place by default.
    if res["imported"]:
        if yes:
            print_info(f"Left the original {src} in place — delete it manually when ready.")
        elif prompt_yes_no(
            f"Delete the old {src} now? Your data is safely copied to {dst}.", default=False
        ):
            if remove_legacy(src):
                print_success(f"Removed {src}.")
            else:
                print_warning(f"Could not remove {src} — delete it manually.")
        else:
            print_info(f"Left {src} in place as a fallback.")
    print_success("Migration complete.")
    return 0
