"""tests/conftest.py must isolate JANUS_HOME before test modules import.

Project modules read the Janus home at *import*: ``tools.approval`` loads
config.yaml's ``command_allowlist`` into its permanent approvals,
``janus_cli.doctor`` / ``cli`` / ``run_agent`` / ``gateway.core`` load
``<home>/.env`` into ``os.environ``, and about 30 modules cache
``get_janus_home()`` in module constants. Test modules import them during
collection, before any fixture runs, so the per-test redirect in
``_hermetic_environment`` comes too late for them.

``scripts/run_tests.sh`` runs ``env -i ... HOME="$HOME"`` with no
JANUS_HOME. Without a collection-time redirect, every one of those imports
resolves the developer's real ``~/.janus``: its approvals and env vars
decide test results that CI (which has no ``~/.janus``) never sees, and
collection writes a home skeleton into it.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from janus_constants import _get_platform_default_janus_home, get_janus_home

REPO_ROOT = Path(__file__).resolve().parent.parent
PROBE = REPO_ROOT / "tests" / "fixtures" / "collection_home_probe.py"

# Captured while pytest imports this module, i.e. at collection time.
_JANUS_HOME_ENV_AT_COLLECTION = os.environ.get("JANUS_HOME")
_JANUS_HOME_AT_COLLECTION = get_janus_home()
_PLATFORM_DEFAULT_HOME = _get_platform_default_janus_home()


def _is_within(path, root) -> bool:
    return Path(path).resolve().is_relative_to(Path(root).resolve())


def _contents(directory):
    """Map each entry name to its bytes (None for a directory)."""
    return {p.name: p.read_bytes() if p.is_file() else None for p in directory.iterdir()}


def test_janus_home_is_isolated_while_this_module_was_imported():
    assert _JANUS_HOME_ENV_AT_COLLECTION, (
        "JANUS_HOME was unset while test modules were imported, so "
        "get_janus_home() fell back to the real home"
    )
    assert not _is_within(_JANUS_HOME_AT_COLLECTION, _PLATFORM_DEFAULT_HOME), (
        f"get_janus_home() at collection was {_JANUS_HOME_AT_COLLECTION}, "
        f"inside the real Janus home {_PLATFORM_DEFAULT_HOME}"
    )


@pytest.mark.timeout(120)
@pytest.mark.parametrize(
    "shell_exports_real_home",
    [False, True],
    ids=["wrapper-env", "shell-exported-real-home"],
)
def test_collection_imports_never_see_the_real_janus_home(
    tmp_path, monkeypatch, shell_exports_real_home
):
    """Run the probe in a fresh pytest whose HOME holds a used Janus home.

    The fake home has an approval and a .env var that must not reach the
    probe's module-scope imports, and it must come out untouched.
    """
    fake_home = tmp_path / "home"
    local_appdata = fake_home / "AppData" / "Local"
    home_vars = {
        "HOME": str(fake_home),
        "USERPROFILE": str(fake_home),
        "LOCALAPPDATA": str(local_appdata),
    }
    # Resolve the platform-native default (~/.janus, or %LOCALAPPDATA%\janus
    # on Windows) the same way the product does, but under the fake home.
    with monkeypatch.context() as mp:
        for var, value in home_vars.items():
            mp.setenv(var, value)
        real_home = _get_platform_default_janus_home()
    assert _is_within(real_home, fake_home)

    real_home.mkdir(parents=True)
    config_yaml = real_home / "config.yaml"
    dot_env = real_home / ".env"
    config_yaml.write_text("command_allowlist:\n  - find -delete\n", encoding="utf-8")
    dot_env.write_text("PROBE_SENTINEL_BASE_URL=https://leak.invalid\n", encoding="utf-8")
    seeded = _contents(real_home)

    out = tmp_path / "snapshot.json"
    # A clean env like scripts/run_tests.sh's ``env -i``. The sentinel and
    # output var names are shared with tests/fixtures/collection_home_probe.py.
    env = {
        "PATH": os.environ.get("PATH", ""),
        **home_vars,
        "TZ": "UTC",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "JANUS_COLLECTION_PROBE_OUT": str(out),
    }
    for var in ("SYSTEMROOT", "TMPDIR", "TEMP", "TMP"):
        if os.environ.get(var):
            env[var] = os.environ[var]
    if shell_exports_real_home:
        # A developer shell (or a profile wrapper) that exports the real
        # home must not win over the conftest redirect under bare pytest.
        env["JANUS_HOME"] = str(real_home)

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(PROBE)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=110,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    snap = json.loads(out.read_text(encoding="utf-8"))

    # Gather every leak, so a failure shows all symptoms at once.
    problems = []
    collection_home = snap["janus_home_env"]
    if not collection_home:
        problems.append("JANUS_HOME was unset while the probe was imported")
    elif _is_within(collection_home, real_home):
        problems.append(f"JANUS_HOME at collection was {collection_home}, the real home")
    elif Path(collection_home).exists():
        problems.append(f"collection-time home {collection_home} was not removed at exit")
    if _is_within(snap["get_janus_home"], real_home):
        problems.append(f"get_janus_home() at collection was {snap['get_janus_home']}")
    if "find -delete" in snap["permanent_approved"]:
        problems.append("tools.approval loaded the real config.yaml command_allowlist")
    if snap["dotenv_sentinel"] is not None:
        problems.append("janus_cli.doctor loaded the real <janus home>/.env")
    after = _contents(real_home)
    if after != seeded:
        added = sorted(set(after) - set(seeded))
        changed = sorted(n for n in seeded if after.get(n) != seeded[n])
        problems.append(f"collection wrote into the real home: added {added}, changed {changed}")
    assert not problems, "\n".join(problems)
