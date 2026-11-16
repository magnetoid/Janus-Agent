"""Tests for inventory._apply_pricing — the pricing enrichment that feeds the
desktop GUI model picker (and onboarding) so it can show $/Mtok columns and
Free badges, the same way the `janus model` CLI picker does.
"""

import janus_cli.inventory as inv
import janus_cli.models as models_mod


def _patch_pricing(monkeypatch, *, pricing):
    monkeypatch.setattr(models_mod, "get_pricing_for_provider", lambda slug, **kw: pricing.get(slug, {}))


def test_apply_pricing_formats_per_model_prices(monkeypatch):
    """Each model gets formatted input/output/cache + a free flag."""
    _patch_pricing(
        monkeypatch,
        pricing={
            "openrouter": {
                "a/paid": {"prompt": "0.000003", "completion": "0.000015", "input_cache_read": "0.0000003"},
                "b/free": {"prompt": "0", "completion": "0"},
            }
        },
    )
    rows = [{"slug": "openrouter", "models": ["a/paid", "b/free"]}]
    inv._apply_pricing(rows)

    pricing = rows[0]["pricing"]
    assert pricing["a/paid"] == {"input": "$3.00", "output": "$15.00", "cache": "$0.30", "free": False}
    assert pricing["b/free"]["free"] is True
    assert pricing["b/free"]["input"] == "free"


def test_apply_pricing_skips_providers_without_pricing(monkeypatch):
    """A provider with no live pricing simply gets no pricing key."""
    _patch_pricing(monkeypatch, pricing={})
    rows = [{"slug": "anthropic", "models": ["claude-x"]}]
    inv._apply_pricing(rows)

    assert "pricing" not in rows[0]


def test_apply_pricing_failure_is_swallowed(monkeypatch):
    """A pricing fetch that raises must not break the whole payload."""
    def boom(slug, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(models_mod, "get_pricing_for_provider", boom)
    rows = [{"slug": "openrouter", "models": ["a/b"]}]
    inv._apply_pricing(rows)  # must not raise

    assert "pricing" not in rows[0]
