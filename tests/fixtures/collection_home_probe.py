"""Collection-time probe for ``tests/test_conftest_collection_home.py``.

Not a test module. Its name has no ``test_`` prefix, so neither pytest's
discovery nor ``scripts/run_tests_parallel.py`` collects it. The outer test
runs it by explicit path in a fresh ``python -m pytest`` subprocess with a
fake HOME, so the repo's ``tests/conftest.py`` loads exactly as in a real run.

Everything at module scope runs while pytest *collects* this file, before
any fixture. That is the window being checked: test modules import project
code there, and project modules read the Janus home at import.
"""

import json
import os

import pytest

import janus_cli.doctor  # noqa: F401  (import loads <janus home>/.env)
import tools.approval  # import loads config.yaml's command_allowlist
from janus_constants import get_janus_home

SENTINEL_ENV_VAR = "PROBE_SENTINEL_BASE_URL"
OUT_ENV_VAR = "JANUS_COLLECTION_PROBE_OUT"

_SNAPSHOT = {
    "janus_home_env": os.environ.get("JANUS_HOME"),
    "get_janus_home": str(get_janus_home()),
    "permanent_approved": sorted(tools.approval._permanent_approved),
    "dotenv_sentinel": os.environ.get(SENTINEL_ENV_VAR),
}


def test_write_collection_snapshot():
    out = os.environ.get(OUT_ENV_VAR)
    if not out:
        pytest.skip("only run by tests/test_conftest_collection_home.py")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(_SNAPSHOT, fh)
