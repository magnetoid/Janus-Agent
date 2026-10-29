"""``janus skills history|diff|undo`` — per-skill version history (plan item 2.5)."""
from __future__ import annotations

import json

from agent import skill_versions as sv


def _disabled() -> int:
    print("  Skill history is disabled. Turn it on with\n"
          "    janus config set skills.versions.enabled true\n"
          "  (versions are recorded from then on).")
    return 1


def _history(args) -> int:
    rows = sv.list_versions(args.name)
    if getattr(args, "json", False):
        print(json.dumps(rows, indent=2, ensure_ascii=False))
        return 0 if rows else 1
    if not rows:
        print(f"  No recorded versions for skill '{args.name}'.")
        return 1
    print(f"\n  {args.name} — {len(rows)} version(s), newest first\n")
    for row in rows:
        print(f"  {row['id']}  {row.get('created_at', '')}  {row.get('actor', '?')}: "
              f"{row.get('reason', '')}  ({row.get('file_count', 0)} files)")
    print(f"\n  diff: janus skills diff {args.name} [id]    "
          f"undo: janus skills undo {args.name} [id]\n")
    return 0


def _diff(args) -> int:
    if not sv.list_versions(args.name):
        print(f"  No recorded versions for skill '{args.name}'.")
        return 1
    text = sv.diff_version(args.name, getattr(args, "version", None))
    print(text if text else "  No differences from that version.")
    return 0


def _undo(args) -> int:
    ok, msg = sv.restore_version(args.name, getattr(args, "version", None),
                                 force=bool(getattr(args, "force", False)))
    print(f"  {'✓' if ok else '✗'} {msg}")
    return 0 if ok else 1


def run(args) -> int:
    if not sv.is_enabled():
        return _disabled()
    action = getattr(args, "skills_action", None)
    if action == "diff":
        return _diff(args)
    if action == "undo":
        return _undo(args)
    return _history(args)
