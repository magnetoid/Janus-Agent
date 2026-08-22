"""Seed bundled eval specs into ``$JANUS_HOME/evals/``.

A slim, file-granular sibling of :mod:`tools.skills_sync` for the committed
``evals/`` suite. The self-improvement promotion gate refuses to run on an
empty suite (``learning.self_improve.min_eval_specs``), so a stock install
must actually HAVE specs — this seeds them at startup and on
``janus evals init``.

Contract (per bundled ``*.yaml``/``*.yml`` file, flat directory):

* **new upstream file** → copied into the home, recorded in the manifest.
* **user-modified copy** (home file's hash differs from the manifest's
  record) → never overwritten.
* **user-deleted copy** (manifest knows the file, home copy is gone) →
  respected; never resurrected.
* **upstream update** to a file the user hasn't touched → copied over,
  manifest updated.
* **stale manifest entries** (upstream no longer ships the file) → dropped;
  the user's copy, if any, is left alone.

Manifest: ``$JANUS_HOME/evals/.bundled_manifest`` — one ``name:md5`` line per
file (hidden, so ``load_eval_specs`` never parses it as a spec).
"""
from __future__ import annotations

import hashlib
import logging
import shutil
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

_MANIFEST_NAME = ".bundled_manifest"


def _file_md5(path: Path) -> str:
    hasher = hashlib.md5()
    hasher.update(path.read_bytes())
    return hasher.hexdigest()


def _bundled_dir() -> Path:
    from janus_constants import get_bundled_evals_dir
    return get_bundled_evals_dir(default=Path(__file__).resolve().parent.parent / "evals")


def _home_dir() -> Path:
    from janus_constants import get_janus_home
    return get_janus_home() / "evals"


def _load_manifest(path: Path) -> Dict[str, str]:
    entries: Dict[str, str] = {}
    try:
        if path.is_file():
            for line in path.read_text(encoding="utf-8").splitlines():
                name, _, digest = line.strip().rpartition(":")
                if name and digest:
                    entries[name] = digest
    except OSError:
        pass
    return entries


def _save_manifest(path: Path, entries: Dict[str, str]) -> None:
    lines = [f"{name}:{digest}" for name, digest in sorted(entries.items())]
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def sync_evals(quiet: bool = False) -> dict:
    """Sync bundled eval specs into the home evals dir. Never raises."""
    summary = {"copied": [], "updated": [], "skipped_modified": [],
               "skipped_deleted": [], "pruned": []}
    try:
        bundled = _bundled_dir()
        if not bundled.is_dir():
            return summary
        home = _home_dir()
        # Don't seed into the bundled dir itself (source checkout where
        # JANUS_HOME points at the repo, or the last-resort fallback).
        if home.resolve() == bundled.resolve():
            return summary
        home.mkdir(parents=True, exist_ok=True)
        manifest_path = home / _MANIFEST_NAME
        manifest = _load_manifest(manifest_path)

        bundled_files = sorted(
            p for p in list(bundled.glob("*.yaml")) + list(bundled.glob("*.yml"))
            if p.is_file() and not p.name.startswith(".")
        )
        bundled_names = {p.name for p in bundled_files}

        for src in bundled_files:
            name = src.name
            dst = home / name
            src_md5 = _file_md5(src)
            recorded = manifest.get(name)
            if recorded is None:
                if dst.exists():
                    # A user file already claims the name — leave it alone and
                    # do NOT adopt it into the manifest (it isn't ours).
                    summary["skipped_modified"].append(name)
                    continue
                shutil.copy2(src, dst)
                manifest[name] = src_md5
                summary["copied"].append(name)
                continue
            if not dst.exists():
                # User deleted their copy — respected, never resurrected.
                summary["skipped_deleted"].append(name)
                continue
            if _file_md5(dst) != recorded:
                # User modified their copy — never overwritten.
                summary["skipped_modified"].append(name)
                continue
            if recorded != src_md5:
                shutil.copy2(src, dst)
                manifest[name] = src_md5
                summary["updated"].append(name)

        for name in list(manifest):
            if name not in bundled_names:
                del manifest[name]
                summary["pruned"].append(name)

        _save_manifest(manifest_path, manifest)
        if not quiet and (summary["copied"] or summary["updated"]):
            logger.info("evals sync: %d copied, %d updated",
                        len(summary["copied"]), len(summary["updated"]))
    except Exception:
        logger.debug("sync_evals failed (continuing)", exc_info=True)
    return summary
