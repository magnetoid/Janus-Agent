"""Unit tests for tools/tool_backend_helpers.py.

Tests cover:
- normalize_browser_cloud_provider() coercion
- has_direct_modal_credentials() detection
- resolve_openai_audio_api_key() priority chain
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from tools.tool_backend_helpers import (
    has_direct_modal_credentials,
    normalize_browser_cloud_provider,
    resolve_openai_audio_api_key,
)


# ---------------------------------------------------------------------------
# normalize_browser_cloud_provider
# ---------------------------------------------------------------------------
class TestNormalizeBrowserCloudProvider:
    """Coerce arbitrary input to a lowercase browser provider key."""

    def test_none_returns_default(self):
        assert normalize_browser_cloud_provider(None) == "local"

    def test_empty_string_returns_default(self):
        assert normalize_browser_cloud_provider("") == "local"

    def test_whitespace_only_returns_default(self):
        assert normalize_browser_cloud_provider("   ") == "local"

    def test_known_provider_normalized(self):
        assert normalize_browser_cloud_provider("BrowserBase") == "browserbase"

    def test_strips_whitespace(self):
        assert normalize_browser_cloud_provider("  Local  ") == "local"

    def test_integer_coerced(self):
        result = normalize_browser_cloud_provider(42)
        assert isinstance(result, str)
        assert result == "42"


# ---------------------------------------------------------------------------
# has_direct_modal_credentials
# ---------------------------------------------------------------------------
class TestHasDirectModalCredentials:
    """Detect Modal credentials via env vars or config file."""

    def test_no_env_no_file(self, monkeypatch, tmp_path):
        monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
        monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
        with patch.object(Path, "home", return_value=tmp_path):
            assert has_direct_modal_credentials() is False

    def test_both_env_vars_set(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MODAL_TOKEN_ID", "id-123")
        monkeypatch.setenv("MODAL_TOKEN_SECRET", "sec-456")
        with patch.object(Path, "home", return_value=tmp_path):
            assert has_direct_modal_credentials() is True

    def test_only_token_id_not_enough(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MODAL_TOKEN_ID", "id-123")
        monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
        with patch.object(Path, "home", return_value=tmp_path):
            assert has_direct_modal_credentials() is False

    def test_only_token_secret_not_enough(self, monkeypatch, tmp_path):
        monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
        monkeypatch.setenv("MODAL_TOKEN_SECRET", "sec-456")
        with patch.object(Path, "home", return_value=tmp_path):
            assert has_direct_modal_credentials() is False

    def test_config_file_present(self, monkeypatch, tmp_path):
        monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
        monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
        (tmp_path / ".modal.toml").touch()
        with patch.object(Path, "home", return_value=tmp_path):
            assert has_direct_modal_credentials() is True

    def test_env_vars_take_priority_over_file(self, monkeypatch, tmp_path):
        monkeypatch.setenv("MODAL_TOKEN_ID", "id-123")
        monkeypatch.setenv("MODAL_TOKEN_SECRET", "sec-456")
        (tmp_path / ".modal.toml").touch()
        with patch.object(Path, "home", return_value=tmp_path):
            assert has_direct_modal_credentials() is True

    def test_home_dir_permission_denied(self, monkeypatch):
        """PermissionError on Path.home() should not crash (issue #33525)."""
        monkeypatch.delenv("MODAL_TOKEN_ID", raising=False)
        monkeypatch.delenv("MODAL_TOKEN_SECRET", raising=False)
        with patch.object(Path, "home", side_effect=PermissionError("denied")):
            assert has_direct_modal_credentials() is False

    def test_home_dir_permission_denied_with_env_vars(self, monkeypatch):
        """PermissionError on Path.home() should not prevent env var detection."""
        monkeypatch.setenv("MODAL_TOKEN_ID", "id-123")
        monkeypatch.setenv("MODAL_TOKEN_SECRET", "sec-456")
        with patch.object(Path, "home", side_effect=PermissionError("denied")):
            assert has_direct_modal_credentials() is True


# ---------------------------------------------------------------------------
# resolve_openai_audio_api_key
# ---------------------------------------------------------------------------
class TestResolveOpenaiAudioApiKey:
    """Priority: VOICE_TOOLS_OPENAI_KEY > OPENAI_API_KEY."""

    def test_voice_key_preferred(self, monkeypatch):
        monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", "voice-key")
        monkeypatch.setenv("OPENAI_API_KEY", "general-key")
        assert resolve_openai_audio_api_key() == "voice-key"

    def test_falls_back_to_openai_key(self, monkeypatch):
        monkeypatch.delenv("VOICE_TOOLS_OPENAI_KEY", raising=False)
        monkeypatch.setenv("OPENAI_API_KEY", "general-key")
        assert resolve_openai_audio_api_key() == "general-key"

    def test_empty_voice_key_falls_back(self, monkeypatch):
        monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", "")
        monkeypatch.setenv("OPENAI_API_KEY", "general-key")
        assert resolve_openai_audio_api_key() == "general-key"

    def test_no_keys_returns_empty(self, monkeypatch):
        monkeypatch.delenv("VOICE_TOOLS_OPENAI_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert resolve_openai_audio_api_key() == ""

    def test_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("VOICE_TOOLS_OPENAI_KEY", "  voice-key  ")
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        assert resolve_openai_audio_api_key() == "voice-key"
