"""Fixtures shared across janus_cli kanban tests."""

from __future__ import annotations

import errno
import importlib
import os
import stat
import subprocess
from pathlib import Path

import pytest

# External programs `janus doctor`, and the helpers it calls, shell out to.
# In doctor tests they act as not installed. `gh auth status` reads the
# developer's GitHub login and can reach the network, `npm audit` queries the
# npm registry, and `docker version` (via docker_usable) asks the local daemon.
DOCTOR_EXTERNAL_CLIS = frozenset({"gh", "docker", "podman", "npm", "ssh", "loginctl"})


def _program_name(args) -> str:
    if isinstance(args, (str, bytes, os.PathLike)):
        text = os.fsdecode(args).strip()
        first = text.split()[0] if text else ""
    else:
        first = os.fsdecode(args[0]) if args else ""
    return Path(first).stem.lower()


class _NoDoctorCliPopen(subprocess.Popen):
    """Popen that refuses to start a DOCTOR_EXTERNAL_CLIS program."""

    def __init__(self, args, *a, **kw):
        name = _program_name(kw.get("executable") or args)
        if name in DOCTOR_EXTERNAL_CLIS:
            raise FileNotFoundError(errno.ENOENT, f"{name} is not run in doctor tests", name)
        super().__init__(args, *a, **kw)


def _command_link_state(home: Path):
    """Identity of ``<home>/.local/bin/janus``, or None when it is absent."""
    link = home / ".local" / "bin" / "janus"
    try:
        st = link.lstat()
    except FileNotFoundError:
        return None
    target = os.readlink(link) if stat.S_ISLNK(st.st_mode) else None
    return (stat.S_IFMT(st.st_mode), st.st_ino, st.st_mtime_ns, target)


@pytest.fixture
def hermetic_doctor(monkeypatch, tmp_path):
    """Run ``janus doctor`` against throwaway paths, never the developer's machine.

    ``run_doctor(--fix)`` creates or re-points ``Path.home()/.local/bin/janus``
    at ``PROJECT_ROOT``'s venv. Unpatched, that is the developer's real command
    and this checkout. So the fixture gives doctor a fake home, a project root
    with no venv or node_modules, the per-test JANUS_HOME (doctor captured the
    real ~/.janus at import), and no DOCTOR_EXTERNAL_CLIS. Tests can override
    any of these with their own monkeypatch. Fails the test if the real
    ``~/.local/bin/janus`` changed anyway.
    """
    from janus_constants import get_janus_home

    # The module run_doctor is called through. Not `import janus_cli.doctor`:
    # tests that delitem it from sys.modules and re-import leave the package
    # attribute pointing at a stale copy.
    doctor_mod = importlib.import_module("janus_cli.doctor")

    real_home = Path.home()
    link_before = _command_link_state(real_home)

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setattr(doctor_mod, "PROJECT_ROOT", tmp_path / "project")
    monkeypatch.setattr(doctor_mod, "JANUS_HOME", get_janus_home())
    monkeypatch.setattr(subprocess, "Popen", _NoDoctorCliPopen)

    yield fake_home

    link_after = _command_link_state(real_home)
    if link_after != link_before:
        pytest.fail(
            f"a doctor test changed {real_home / '.local' / 'bin' / 'janus'} "
            f"({link_before!r} -> {link_after!r}); run_doctor must only ever "
            "see the fake home from the hermetic_doctor fixture",
            pytrace=False,
        )


@pytest.fixture
def all_assignees_spawnable(monkeypatch):
    """Pretend every assignee maps to a real Janus profile.

    Most dispatcher tests use synthetic assignees ("alice", "bob") that
    don't correspond to actual profile directories on disk. Without this
    patch, the dispatcher's profile-exists guard (PR #20105) routes
    those tasks into ``skipped_nonspawnable`` instead of spawning, which
    would break tests that assert spawn behavior.
    """
    from janus_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)


@pytest.fixture(autouse=True)
def _suppress_concurrent_janus_gate(request, monkeypatch):
    """Default ``_detect_concurrent_janus_instances`` to ``[]`` for every test.

    The Windows update path now refuses to proceed when another
    ``janus.exe`` is detected (issue #26670). On a developer's Windows
    machine running the test suite via ``janus`` itself, this would
    flag the running agent as a concurrent instance and abort every
    ``cmd_update`` test. Tests that want to exercise the gate explicitly
    re-patch ``_detect_concurrent_janus_instances`` with their own
    return value — autouse here gives a clean default without touching
    the rest of the suite.

    Tests that need to call the REAL function (e.g. unit tests for the
    helper itself) opt out with ``@pytest.mark.real_concurrent_gate``.
    """
    if request.node.get_closest_marker("real_concurrent_gate"):
        return
    try:
        from janus_cli import main as _cli_main
    except Exception:
        return
    monkeypatch.setattr(
        _cli_main, "_detect_concurrent_janus_instances", lambda *_a, **_k: []
    )
