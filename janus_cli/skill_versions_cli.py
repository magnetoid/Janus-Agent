"""`janus skills history|diff|undo|archive|restore` and their `/skills` slash form.

Thin shell around :mod:`agent.skill_versions`. Every verb takes an ``emit``
callable so the argparse path prints to stdout and the slash path prints
through the chat console (with Rich markup off — diffs are full of brackets).
"""

from __future__ import annotations

from typing import Callable, List, Optional

Emit = Callable[[str], None]

VERSION_VERBS = ("history", "diff", "undo", "archive", "restore")

USAGE = {
    "history": "history <skill>",
    "diff": "diff <skill> [<version>]",
    "undo": "undo <skill> [<version>]",
    "archive": "archive <skill>",
    "restore": "restore <skill>",
}


def _short_time(iso: str) -> str:
    return (iso or "?")[:19].replace("T", " ")


def cmd_history(name: str, emit: Emit = print) -> int:
    from agent import skill_versions as sv
    try:
        rows = sv.list_versions(name)
    except sv.SkillVersionError as e:
        emit(f"skills: {e}")
        return 1
    if not rows:
        emit(f"No recorded versions for skill '{name}'.")
        return 0
    emit(f"Version history for '{name}' (newest first; each is the state BEFORE that write):")
    header = f"{'#':>3}  {'id':<40}  {'when (UTC)':<19}  {'source':<12}  {'files':>5}  {'size':>8}  reason"
    emit(header)
    emit("─" * len(header))
    for r in rows:
        files = "absent" if r.get("absent") else str(r.get("files", 0))
        flag = " [archived]" if r.get("archived") else ""
        emit(
            f"{r['n']:>3}  {r['id']:<40}  {_short_time(r.get('created_at', '')):<19}  "
            f"{r.get('source', '?'):<12}  {files:>5}  "
            f"{sv.format_size(int(r.get('bytes', 0))):>8}  {r.get('reason', '')}{flag}"
        )
    emit("")
    emit(f"Restore one with `janus skills undo {name} <#|id>`; compare with `janus skills diff {name} <#|id>`.")
    return 0


def cmd_diff(name: str, version: Optional[str] = None, emit: Emit = print) -> int:
    from agent import skill_versions as sv
    try:
        entry, text = sv.diff(name, version)
    except sv.SkillVersionError as e:
        emit(f"skills: {e}")
        return 1
    if not text:
        emit(f"'{name}' is identical to version {entry['id']}.")
        return 0
    emit(text.rstrip("\n"))
    return 0


def cmd_undo(name: str, version: Optional[str] = None, emit: Emit = print) -> int:
    from agent import skill_versions as sv
    try:
        result = sv.undo(name, version)
    except sv.SkillVersionError as e:
        emit(f"skills: undo refused — {e}")
        return 1
    restored = result["restored"]
    if restored.get("absent"):
        emit(f"skills: '{name}' did not exist at version {restored['id']}; "
             f"moved the current copy into history as {result['recorded']}.")
    else:
        emit(f"skills: restored '{name}' to version {restored['id']} at {result['path']}.")
    emit(f"  The previous state was saved as version {result['recorded']} "
         f"(run `janus skills undo {name}` again to redo).")
    emit("  Run /reload-skills in an open session if the skill's description changed.")
    return 0


def cmd_archive(name: str, emit: Emit = print) -> int:
    from agent import skill_versions as sv
    try:
        msg = sv.archive(name)
    except sv.SkillVersionError as e:
        emit(f"skills: archive refused — {e}")
        return 1
    emit(f"skills: {msg}")
    return 0


def cmd_restore(name: str, emit: Emit = print) -> int:
    from agent import skill_versions as sv
    try:
        msg = sv.restore(name)
    except sv.SkillVersionError as e:
        emit(f"skills: restore refused — {e}")
        return 1
    emit(f"skills: {msg}")
    return 0


def dispatch(verb: str, args: List[str], emit: Emit = print) -> int:
    """Run a version verb from a token list (the slash-command path)."""
    positional = [a for a in args if not a.startswith("-")]
    if verb not in VERSION_VERBS:
        emit(f"skills: unknown version command '{verb}'")
        return 2
    if not positional:
        emit(f"Usage: /skills {USAGE[verb]}")
        return 2
    name = positional[0]
    version = positional[1] if len(positional) > 1 else None
    if verb == "history":
        return cmd_history(name, emit)
    if verb == "diff":
        return cmd_diff(name, version, emit)
    if verb == "undo":
        return cmd_undo(name, version, emit)
    if verb == "archive":
        return cmd_archive(name, emit)
    return cmd_restore(name, emit)


def run_from_args(args) -> int:
    """Run a version verb from parsed argparse ``args`` (``janus skills …``)."""
    verb = getattr(args, "skills_action", None) or ""
    name = getattr(args, "skill", "")
    version = getattr(args, "version_ref", None)
    if verb == "history":
        return cmd_history(name)
    if verb == "diff":
        return cmd_diff(name, version)
    if verb == "undo":
        return cmd_undo(name, version)
    if verb == "archive":
        return cmd_archive(name)
    if verb == "restore":
        return cmd_restore(name)
    print(f"skills: unknown version command '{verb}'")
    return 2


def register_subparsers(skills_subparsers) -> None:
    """Add the version verbs to the ``janus skills`` argparse subparsers."""
    p = skills_subparsers.add_parser(
        "history", help="List a skill's recorded versions (newest first)")
    p.add_argument("skill", help="Skill name")

    p = skills_subparsers.add_parser(
        "diff", help="Diff a recorded version (default: newest) against the current skill")
    p.add_argument("skill", help="Skill name")
    # dest must not be "version": that is the top-level `janus --version` flag.
    p.add_argument("version_ref", metavar="version", nargs="?", default=None,
                   help="Version id, unique id prefix, or history position (1 = newest)")

    p = skills_subparsers.add_parser(
        "undo", help="Restore a skill to a recorded version (the undo is itself recorded)")
    p.add_argument("skill", help="Skill name")
    # dest must not be "version": that is the top-level `janus --version` flag.
    p.add_argument("version_ref", metavar="version", nargs="?", default=None,
                   help="Version id, unique id prefix, or history position (default: newest)")

    p = skills_subparsers.add_parser(
        "archive", help="Move a skill into skills/.archive/ (refuses pinned skills)")
    p.add_argument("skill", help="Skill name")

    p = skills_subparsers.add_parser(
        "restore", help="Bring an archived skill back into the active tree")
    p.add_argument("skill", help="Skill name")
