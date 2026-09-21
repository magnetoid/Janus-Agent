"""Files a Janus reply hands to Blob — which ones, and why a file sometimes is not.

Blob has no attachment API a Janus run could call: the Janus that runs beside Blob holds
no Blob credential (Blob calls it, never the reverse), and a Janus on a laptop has no
address to be fetched from. So a file travels inside the run it belongs to, as
`agui.file_events`, over whichever connector carried the run. This module chooses the
files: the ones a reply names, by the rules every other platform uses — `extract_media`
for `MEDIA:` tags, `extract_local_files` for bare paths, `validate_media_delivery_path`
for what may leave this machine — plus one rule of Blob's own.

**Nothing inside Janus's own state is ever attached.** Janus sits in every public channel
of a Blob workspace and in people's DMs, so whoever can talk to it in one channel can ask
it for a file, and the Janus home holds every other conversation's session, its
memories, its logs and its tokens. The core denylist covers the credential files; the
state beside them changes on every turn, which is exactly what strict mode's "recently
produced" trust lets through. So under the Janus home, what Janus keeps for itself is
refused by name, and what an agent makes in a folder of its own is not — the file that
started this was `/opt/data/hadley/index.html`.

A file named with `MEDIA:` that cannot be sent says so, in a line added to the reply. A
bare path that does not qualify is left as the words it was: the reply may have been
talking about a file rather than handing it over.
"""

from __future__ import annotations

import logging
import mimetypes
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from gateway.platforms.base import BasePlatformAdapter, validate_media_delivery_path
from janus_constants import get_default_janus_root, get_janus_home

logger = logging.getLogger(__name__)

#: Blob's defaults: ten files per run and 25 MiB of them in all (`AGUI_MAX_FILE_BYTES`).
#: Sending more than Blob keeps only fills the stream with bytes it will drop.
MAX_FILES = 10
MAX_FILE_BYTES = 25 * 1024 * 1024

#: Top-level names under the Janus home that are Janus's own, never a deliverable.
JANUS_STATE = frozenset(
    {
        ".env", "auth.json", "credentials", "config.yaml", "state.db", "sessions",
        "memories", "learning", "logs", "backups", "profiles", "cron", "platforms",
        "whatsapp", "pastes", "evals", "page_memory", "home", "skills", "plugins",
        "scripts", "workflows", "bin", "node", "node_modules",
    }
)  # fmt: skip

#: Top-level files the state list cannot name one by one: databases and their journals,
#: tokens and pairings, and every dotfile the updater and the gateway leave behind.
_STATE_SUFFIXES = (".db", ".db-wal", ".db-shm", ".json", ".yaml", ".yml", ".log")

#: Python's own table, not the host's `/etc/mime.types`: the same name gets the same type
#: on a laptop and in the container.
_TYPES = mimetypes.MimeTypes(filenames=())


@dataclass
class Deliverable:
    name: str
    mime: str
    data: bytes


@dataclass
class Handed:
    """What a reply hands over, what it could not, and the text left to send."""

    files: List[Deliverable] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    text: str = ""

    def text_with_notes(self) -> str:
        """The reply, with a line for each file that was named and could not be sent."""
        lines = [f"_{note}_" for note in self.notes]
        return "\n\n".join(part for part in (self.text, "\n".join(lines)) if part)


def collect(text: str) -> Handed:
    """The files `text` hands over, read into memory, and the text without its tags.

    Blocking file I/O: call it off the event loop.
    """
    media, cleaned = BasePlatformAdapter.extract_media(text or "")
    named = [str(path) for path, _voice in media]
    # The bare paths are found in the text but not cut from it: on Blob the file lands
    # right under the sentence, and "File: — a single page" reads as a mistake.
    bare, _unused = BasePlatformAdapter.extract_local_files(cleaned)
    handed = Handed(text=cleaned.strip())

    seen: set = set()
    total = 0
    said_too_many = False
    for raw, explicit in [(p, True) for p in named] + [(str(p), False) for p in bare]:
        name = Path(raw.rstrip("/")).name or raw
        refusal = _refusal(raw)
        if refusal is not None:
            if explicit:
                handed.notes.append(f"Couldn't attach {name}: {refusal}")
            logger.info("blob: not attaching %s: %s", name, refusal)
            continue
        safe = validate_media_delivery_path(raw)
        assert safe is not None  # `_refusal` said it qualifies
        if safe in seen:
            continue
        seen.add(safe)
        if len(handed.files) >= MAX_FILES:
            if not said_too_many:
                said_too_many = True
                handed.notes.append(f"Only the first {MAX_FILES} files were attached.")
            continue
        size = os.path.getsize(safe)
        if total + size > MAX_FILE_BYTES:
            handed.notes.append(f"Couldn't attach {name}: it's larger than a chat can take.")
            continue
        data = Path(safe).read_bytes()
        total += len(data)
        handed.files.append(Deliverable(name=Path(safe).name, mime=_type_of(safe), data=data))
    return handed


def _refusal(raw: str) -> Optional[str]:
    """Why this path may not be attached, in words for the reply, or None."""
    try:
        exists = Path(os.path.expanduser(raw)).is_file()
    except (OSError, ValueError):
        exists = False
    if not exists:
        return "it isn't there."
    safe = validate_media_delivery_path(raw)
    if safe is None:
        return "that file can't leave this machine."
    if _is_janus_state(Path(safe)):
        return "it's part of Janus's own state."
    return None


def _is_janus_state(path: Path) -> bool:
    for root in {get_janus_home(), get_default_janus_root()}:
        try:
            relative = path.resolve().relative_to(Path(root).expanduser().resolve())
        except (ValueError, OSError, RuntimeError):
            continue
        first = relative.parts[0] if relative.parts else ""
        if (
            not first
            or first in JANUS_STATE
            or first.startswith(".")
            or (len(relative.parts) == 1 and first.endswith(_STATE_SUFFIXES))
        ):
            return True
    return False


def _type_of(path: str) -> str:
    guessed, _ = _TYPES.guess_type(path, strict=False)
    return guessed or "application/octet-stream"
