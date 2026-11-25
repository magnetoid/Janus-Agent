"""opencode-go in the /model picker: listed when its key is set, with its curated models.

The picker puts models.dev entries in front of the curated list for
opencode-go and shows at most ``max_models`` of them. These tests decide
what models.dev returns (nothing, or a couple of extra models) instead of
reading the live registry: with the network fetch, the outcome depended on
how many models models.dev listed that day, and the test broke once the
live entries pushed curated ones past the cap.
"""

import socket

import pytest

from janus_cli.model_switch import list_authenticated_providers
from janus_cli.models import _PROVIDER_MODELS


def _models_dev_lists(monkeypatch, lists):
    """Make models.dev list *lists[provider]* agentic models for a provider."""
    monkeypatch.setattr(
        "agent.models_dev.list_agentic_models",
        lambda provider: list(lists.get(provider, [])),
    )


@pytest.fixture(autouse=True)
def _offline_catalog(monkeypatch):
    """No network and no cached or live catalog: only what a test hands in."""
    def _refuse(*_a, **_k):
        raise OSError("network disabled in this test")

    def _refuse_lookup(*_a, **_k):
        raise socket.gaierror("network disabled in this test")

    monkeypatch.setattr(socket.socket, "connect", _refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse)
    monkeypatch.setattr(socket, "getaddrinfo", _refuse_lookup)
    # The provider scan checks Copilot too, which would read `gh auth token`.
    monkeypatch.setattr("janus_cli.copilot_auth._try_gh_cli_token",
                        lambda *a, **k: None)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda *a, **k: {})
    monkeypatch.setattr("janus_cli.models.cached_provider_model_ids",
                        lambda *a, **k: [])
    _models_dev_lists(monkeypatch, {})


def _opencode_go():
    providers = list_authenticated_providers(current_provider="openrouter", max_models=50)
    return next((p for p in providers if p["slug"] == "opencode-go"), None)


def test_opencode_go_lists_its_curated_models_when_the_key_is_set(monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "test-key")

    entry = _opencode_go()

    assert entry is not None, "opencode-go should appear when OPENCODE_GO_API_KEY is set"
    assert set(_PROVIDER_MODELS["opencode-go"]) <= set(entry["models"])
    assert entry["source"] in {"built-in", "janus"}


def test_models_dev_entries_are_added_to_the_curated_models(monkeypatch):
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "test-key")
    _models_dev_lists(monkeypatch, {"opencode-go": ["fresh-model-a", "fresh-model-b"]})

    entry = _opencode_go()

    assert entry is not None
    shown = set(entry["models"])
    assert {"fresh-model-a", "fresh-model-b"} <= shown
    assert set(_PROVIDER_MODELS["opencode-go"]) <= shown


def test_opencode_go_is_not_listed_without_credentials(monkeypatch):
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)

    assert _opencode_go() is None, "opencode-go should not appear without credentials"
