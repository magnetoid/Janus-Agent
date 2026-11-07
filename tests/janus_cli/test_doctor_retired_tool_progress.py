"""``janus doctor`` folds the retired tool-progress settings into display.platforms."""

from janus_cli.doctor import _fold_legacy_tool_progress


def test_leftover_map_moves_into_platforms_without_clobbering():
    config = {
        "display": {
            "tool_progress_overrides": {"signal": "off", "telegram": "verbose"},
            "platforms": {"telegram": {"tool_progress": "new"}},
        }
    }

    moved = _fold_legacy_tool_progress(config)

    assert moved == ["signal"]
    assert "tool_progress_overrides" not in config["display"]
    assert config["display"]["platforms"]["signal"] == {"tool_progress": "off"}
    # An explicit display.platforms value always wins over the retired map.
    assert config["display"]["platforms"]["telegram"] == {"tool_progress": "new"}


def test_config_without_leftovers_is_untouched():
    config = {"display": {"tool_progress": "all"}}

    assert _fold_legacy_tool_progress(config) is None
    assert config == {"display": {"tool_progress": "all"}}
