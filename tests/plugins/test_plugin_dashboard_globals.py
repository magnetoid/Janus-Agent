"""Bundled dashboard plugins must read the globals the dashboard host defines.

The host (``web/src/plugins/registry.ts``) exposes the plugin registry and SDK
as ``window.__JANUS_*__`` globals. A bundle that reads any other name gets
``undefined`` and returns early, so its page is silently blank. The kanban and
achievements bundles shipped that way, reading the pre-rename globals.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_HOST = _ROOT / "web" / "src" / "plugins" / "registry.ts"
_BUNDLES = sorted((_ROOT / "plugins").glob("*/dashboard/dist/*.js"))
_GLOBAL = re.compile(r"window\.(__[A-Z0-9_]+__)")


def _host_globals() -> set[str]:
    return set(re.findall(r"window\.(__[A-Z0-9_]+__)\s*=", _HOST.read_text(encoding="utf-8")))


def test_the_host_defines_plugin_globals():
    assert _host_globals(), f"no window.__X__ assignments found in {_HOST}"
    assert _BUNDLES, "no plugin dashboard bundles found"


@pytest.mark.parametrize("bundle", _BUNDLES, ids=lambda p: str(p.relative_to(_ROOT)))
def test_bundle_reads_only_host_globals(bundle):
    code = bundle.read_text(encoding="utf-8")
    read = set(_GLOBAL.findall(code))

    assert read, f"{bundle.name} reads no host globals; is it a dashboard plugin?"
    assert read <= _host_globals(), f"undefined globals: {sorted(read - _host_globals())}"


def _manifest_names() -> dict[Path, str]:
    import json

    return {
        b: json.loads((b.parents[1] / "manifest.json").read_text(encoding="utf-8"))["name"]
        for b in _BUNDLES
        if (b.parents[1] / "manifest.json").is_file()
    }


@pytest.mark.parametrize("bundle", _BUNDLES, ids=lambda p: str(p.relative_to(_ROOT)))
def test_bundle_registers_under_its_manifest_name(bundle):
    names = re.findall(r"\.register\(\s*[\"']([^\"']+)[\"']", bundle.read_text(encoding="utf-8"))
    expected = _manifest_names().get(bundle)
    if expected is None or not names:
        pytest.skip("not a registering entry bundle")

    assert names == [expected] * len(names)
