"""
Multi-provider authentication system for Janus Agent.

Supports OAuth flows (OpenAI Codex, xAI, MiniMax, Qwen, Spotify, ...) and
traditional API key providers (OpenRouter, custom endpoints). Auth state
is persisted in ~/.janus/auth.json with cross-process file locking.

Architecture:
- ProviderConfig registry defines known OAuth providers
- Auth store (auth.json) holds per-provider credential state
- resolve_provider() picks the active provider via priority chain
- resolve_*_runtime_credentials() handles token refresh and runtime keys
- logout_command() is the CLI entry point for clearing auth
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import shlex
import stat
import sys
import base64
import hashlib
import subprocess
import threading
import time
import uuid
import webbrowser
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, List, Optional
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from janus_cli.config import get_janus_home, get_config_path, read_raw_config
from janus_constants import OPENROUTER_BASE_URL, secure_parent_dir
from agent.credential_persistence import sanitize_borrowed_credential_payload
from utils import atomic_replace, atomic_yaml_write

logger = logging.getLogger(__name__)

try:
    import fcntl
except Exception:
    fcntl = None
try:
    import msvcrt
except Exception:
    msvcrt = None

# =============================================================================
# Constants
# =============================================================================

AUTH_STORE_VERSION = 1
AUTH_LOCK_TIMEOUT_SECONDS = 15.0

DEFAULT_CODEX_BASE_URL = "https://chatgpt.com/backend-api/codex"
DEFAULT_XAI_OAUTH_BASE_URL = "https://api.x.ai/v1"
MINIMAX_OAUTH_CLIENT_ID = "78257093-7e40-4613-99e0-527b14b39113"
MINIMAX_OAUTH_SCOPE = "group_id profile model.completion"
MINIMAX_OAUTH_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:user_code"
MINIMAX_OAUTH_GLOBAL_BASE = "https://api.minimax.io"
MINIMAX_OAUTH_CN_BASE = "https://api.minimaxi.com"
MINIMAX_OAUTH_GLOBAL_INFERENCE = "https://api.minimax.io/anthropic"
MINIMAX_OAUTH_CN_INFERENCE = "https://api.minimaxi.com/anthropic"
MINIMAX_OAUTH_REFRESH_SKEW_SECONDS = 60
DEFAULT_QWEN_BASE_URL = "https://portal.qwen.ai/v1"
DEFAULT_GITHUB_MODELS_BASE_URL = "https://api.githubcopilot.com"
DEFAULT_COPILOT_ACP_BASE_URL = "acp://copilot"
DEFAULT_OLLAMA_CLOUD_BASE_URL = "https://ollama.com/v1"
STEPFUN_STEP_PLAN_INTL_BASE_URL = "https://api.stepfun.ai/step_plan/v1"
STEPFUN_STEP_PLAN_CN_BASE_URL = "https://api.stepfun.com/step_plan/v1"
CODEX_OAUTH_CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
CODEX_OAUTH_TOKEN_URL = "https://auth.openai.com/oauth/token"
CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120
XAI_OAUTH_ISSUER = "https://auth.x.ai"
XAI_OAUTH_DISCOVERY_URL = f"{XAI_OAUTH_ISSUER}/.well-known/openid-configuration"
XAI_OAUTH_CLIENT_ID = "b1a00492-073a-47ea-816f-4c329264a828"
XAI_OAUTH_SCOPE = "openid profile email offline_access grok-cli:access api:access"
XAI_OAUTH_REDIRECT_HOST = "127.0.0.1"
XAI_OAUTH_REDIRECT_PORT = 56121
XAI_OAUTH_REDIRECT_PATH = "/callback"
XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120
QWEN_OAUTH_CLIENT_ID = "f0304373b74a44d2b584a3fb70ca9e56"
QWEN_OAUTH_TOKEN_URL = "https://chat.qwen.ai/api/v1/oauth2/token"
QWEN_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120
DEFAULT_SPOTIFY_ACCOUNTS_BASE_URL = "https://accounts.spotify.com"
DEFAULT_SPOTIFY_API_BASE_URL = "https://api.spotify.com/v1"
DEFAULT_SPOTIFY_REDIRECT_URI = "http://127.0.0.1:43827/spotify/callback"
SPOTIFY_DOCS_URL = "https://github.com/magnetoid/janus"
SPOTIFY_DASHBOARD_URL = "https://developer.spotify.com/dashboard"
SPOTIFY_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 120

XAI_OAUTH_DOCS_URL = "https://github.com/magnetoid/janus"
OAUTH_OVER_SSH_DOCS_URL = "https://github.com/magnetoid/janus"
DEFAULT_SPOTIFY_SCOPE = " ".join((
    "user-modify-playback-state",
    "user-read-playback-state",
    "user-read-currently-playing",
    "user-read-recently-played",
    "playlist-read-private",
    "playlist-read-collaborative",
    "playlist-modify-public",
    "playlist-modify-private",
    "user-library-read",
    "user-library-modify",
))
SERVICE_PROVIDER_NAMES: Dict[str, str] = {
    "spotify": "Spotify",
}

# Google Gemini OAuth (google-gemini-cli provider, Cloud Code Assist backend)
DEFAULT_GEMINI_CLOUDCODE_BASE_URL = "cloudcode-pa://google"
GEMINI_OAUTH_ACCESS_TOKEN_REFRESH_SKEW_SECONDS = 60  # refresh 60s before expiry

# LM Studio's default no-auth mode still requires *some* non-empty bearer for
# the API-key code paths (auxiliary_client, runtime resolver) to treat the
# provider as configured. This sentinel is sent only to LM Studio, never to
# any remote service.
LMSTUDIO_NOAUTH_PLACEHOLDER = "dummy-lm-api-key"


# =============================================================================
# Provider Registry
# =============================================================================

@dataclass
class ProviderConfig:
    """Describes a known inference provider."""
    id: str
    name: str
    auth_type: str  # "oauth_external", "oauth_minimax", or "api_key"
    portal_base_url: str = ""
    inference_base_url: str = ""
    client_id: str = ""
    scope: str = ""
    extra: Dict[str, Any] = field(default_factory=dict)
    # For API-key providers: env vars to check (in priority order)
    api_key_env_vars: tuple = ()
    # Optional env var for base URL override
    base_url_env_var: str = ""


PROVIDER_REGISTRY: Dict[str, ProviderConfig] = {
    "openai-codex": ProviderConfig(
        id="openai-codex",
        name="OpenAI Codex",
        auth_type="oauth_external",
        inference_base_url=DEFAULT_CODEX_BASE_URL,
    ),
    "openai-api": ProviderConfig(
        id="openai-api",
        name="OpenAI API",
        auth_type="api_key",
        inference_base_url="https://api.openai.com/v1",
        api_key_env_vars=("OPENAI_API_KEY",),
        base_url_env_var="OPENAI_BASE_URL",
    ),
    "xai-oauth": ProviderConfig(
        id="xai-oauth",
        name="xAI Grok OAuth (SuperGrok / Premium+)",
        auth_type="oauth_external",
        inference_base_url=DEFAULT_XAI_OAUTH_BASE_URL,
    ),
    "qwen-oauth": ProviderConfig(
        id="qwen-oauth",
        name="Qwen OAuth",
        auth_type="oauth_external",
        inference_base_url=DEFAULT_QWEN_BASE_URL,
    ),
    "google-gemini-cli": ProviderConfig(
        id="google-gemini-cli",
        name="Google Gemini (OAuth)",
        auth_type="oauth_external",
        inference_base_url=DEFAULT_GEMINI_CLOUDCODE_BASE_URL,
    ),
    "lmstudio": ProviderConfig(
        id="lmstudio",
        name="LM Studio",
        auth_type="api_key",
        inference_base_url="http://127.0.0.1:1234/v1",
        api_key_env_vars=("LM_API_KEY",),
        base_url_env_var="LM_BASE_URL",
    ),
    "copilot": ProviderConfig(
        id="copilot",
        name="GitHub Copilot",
        auth_type="api_key",
        inference_base_url=DEFAULT_GITHUB_MODELS_BASE_URL,
        api_key_env_vars=("COPILOT_GITHUB_TOKEN", "GH_TOKEN", "GITHUB_TOKEN"),
        base_url_env_var="COPILOT_API_BASE_URL",
    ),
    "copilot-acp": ProviderConfig(
        id="copilot-acp",
        name="GitHub Copilot ACP",
        auth_type="external_process",
        inference_base_url=DEFAULT_COPILOT_ACP_BASE_URL,
        base_url_env_var="COPILOT_ACP_BASE_URL",
    ),
    "gemini": ProviderConfig(
        id="gemini",
        name="Google AI Studio",
        auth_type="api_key",
        inference_base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key_env_vars=("GOOGLE_API_KEY", "GEMINI_API_KEY"),
        base_url_env_var="GEMINI_BASE_URL",
    ),
    "zai": ProviderConfig(
        id="zai",
        name="Z.AI / GLM",
        auth_type="api_key",
        inference_base_url="https://api.z.ai/api/paas/v4",
        api_key_env_vars=("GLM_API_KEY", "ZAI_API_KEY", "Z_AI_API_KEY"),
        base_url_env_var="GLM_BASE_URL",
    ),
    "kimi-coding": ProviderConfig(
        id="kimi-coding",
        name="Kimi / Moonshot",
        auth_type="api_key",
        # Legacy platform.moonshot.ai keys use this endpoint (OpenAI-compat).
        # sk-kimi- (Kimi Code) keys are auto-redirected to api.kimi.com/coding
        # by _resolve_kimi_base_url() below.
        inference_base_url="https://api.moonshot.ai/v1",
        api_key_env_vars=("KIMI_API_KEY", "KIMI_CODING_API_KEY"),
        base_url_env_var="KIMI_BASE_URL",
    ),
    "kimi-coding-cn": ProviderConfig(
        id="kimi-coding-cn",
        name="Kimi / Moonshot (China)",
        auth_type="api_key",
        inference_base_url="https://api.moonshot.cn/v1",
        api_key_env_vars=("KIMI_CN_API_KEY",),
    ),
    "stepfun": ProviderConfig(
        id="stepfun",
        name="StepFun Step Plan",
        auth_type="api_key",
        inference_base_url=STEPFUN_STEP_PLAN_INTL_BASE_URL,
        api_key_env_vars=("STEPFUN_API_KEY",),
        base_url_env_var="STEPFUN_BASE_URL",
    ),
    "arcee": ProviderConfig(
        id="arcee",
        name="Arcee AI",
        auth_type="api_key",
        inference_base_url="https://api.arcee.ai/api/v1",
        api_key_env_vars=("ARCEEAI_API_KEY",),
        base_url_env_var="ARCEE_BASE_URL",
    ),
    "gmi": ProviderConfig(
        id="gmi",
        name="GMI Cloud",
        auth_type="api_key",
        inference_base_url="https://api.gmi-serving.com/v1",
        api_key_env_vars=("GMI_API_KEY",),
        base_url_env_var="GMI_BASE_URL",
    ),
    "minimax": ProviderConfig(
        id="minimax",
        name="MiniMax",
        auth_type="api_key",
        inference_base_url="https://api.minimax.io/anthropic",
        api_key_env_vars=("MINIMAX_API_KEY",),
        base_url_env_var="MINIMAX_BASE_URL",
    ),
    "minimax-oauth": ProviderConfig(
        id="minimax-oauth",
        name="MiniMax (OAuth \u00b7 minimax.io)",
        auth_type="oauth_minimax",
        portal_base_url=MINIMAX_OAUTH_GLOBAL_BASE,
        inference_base_url=MINIMAX_OAUTH_GLOBAL_INFERENCE,
        client_id=MINIMAX_OAUTH_CLIENT_ID,
        scope=MINIMAX_OAUTH_SCOPE,
        extra={"region": "global", "cn_portal_base_url": MINIMAX_OAUTH_CN_BASE,
               "cn_inference_base_url": MINIMAX_OAUTH_CN_INFERENCE},
    ),
    "anthropic": ProviderConfig(
        id="anthropic",
        name="Anthropic",
        auth_type="api_key",
        inference_base_url="https://api.anthropic.com",
        api_key_env_vars=("ANTHROPIC_API_KEY", "ANTHROPIC_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"),
        base_url_env_var="ANTHROPIC_BASE_URL",
    ),
    "alibaba": ProviderConfig(
        id="alibaba",
        name="Qwen Cloud",
        auth_type="api_key",
        inference_base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        api_key_env_vars=("DASHSCOPE_API_KEY",),
        base_url_env_var="DASHSCOPE_BASE_URL",
    ),
    "alibaba-coding-plan": ProviderConfig(
        id="alibaba-coding-plan",
        name="Alibaba Cloud (Coding Plan)",
        auth_type="api_key",
        inference_base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
        api_key_env_vars=("ALIBABA_CODING_PLAN_API_KEY", "DASHSCOPE_API_KEY"),
        base_url_env_var="ALIBABA_CODING_PLAN_BASE_URL",
    ),
    "minimax-cn": ProviderConfig(
        id="minimax-cn",
        name="MiniMax (China)",
        auth_type="api_key",
        inference_base_url="https://api.minimaxi.com/anthropic",
        api_key_env_vars=("MINIMAX_CN_API_KEY",),
        base_url_env_var="MINIMAX_CN_BASE_URL",
    ),
    "deepseek": ProviderConfig(
        id="deepseek",
        name="DeepSeek",
        auth_type="api_key",
        inference_base_url="https://api.deepseek.com/v1",
        api_key_env_vars=("DEEPSEEK_API_KEY",),
        base_url_env_var="DEEPSEEK_BASE_URL",
    ),
    "xai": ProviderConfig(
        id="xai",
        name="xAI",
        auth_type="api_key",
        inference_base_url="https://api.x.ai/v1",
        api_key_env_vars=("XAI_API_KEY",),
        base_url_env_var="XAI_BASE_URL",
    ),
    "nvidia": ProviderConfig(
        id="nvidia",
        name="NVIDIA NIM",
        auth_type="api_key",
        inference_base_url="https://integrate.api.nvidia.com/v1",
        api_key_env_vars=("NVIDIA_API_KEY",),
        base_url_env_var="NVIDIA_BASE_URL",
    ),
    "opencode-zen": ProviderConfig(
        id="opencode-zen",
        name="OpenCode Zen",
        auth_type="api_key",
        inference_base_url="https://opencode.ai/zen/v1",
        api_key_env_vars=("OPENCODE_ZEN_API_KEY",),
        base_url_env_var="OPENCODE_ZEN_BASE_URL",
    ),
    "opencode-go": ProviderConfig(
        id="opencode-go",
        name="OpenCode Go",
        auth_type="api_key",
        # OpenCode Go mixes API surfaces by model:
        # - GLM / Kimi use OpenAI-compatible chat completions under /v1
        # - MiniMax models use Anthropic Messages under /v1/messages
        # - Qwen 3.7 uses Anthropic Messages under /v1/messages
        # Keep the provider base at /v1 and select api_mode per-model.
        inference_base_url="https://opencode.ai/zen/go/v1",
        api_key_env_vars=("OPENCODE_GO_API_KEY",),
        base_url_env_var="OPENCODE_GO_BASE_URL",
    ),
    "kilocode": ProviderConfig(
        id="kilocode",
        name="Kilo Code",
        auth_type="api_key",
        inference_base_url="https://api.kilo.ai/api/gateway",
        api_key_env_vars=("KILOCODE_API_KEY",),
        base_url_env_var="KILOCODE_BASE_URL",
    ),
    "huggingface": ProviderConfig(
        id="huggingface",
        name="Hugging Face",
        auth_type="api_key",
        inference_base_url="https://router.huggingface.co/v1",
        api_key_env_vars=("HF_TOKEN",),
        base_url_env_var="HF_BASE_URL",
    ),
    "xiaomi": ProviderConfig(
        id="xiaomi",
        name="Xiaomi MiMo",
        auth_type="api_key",
        inference_base_url="https://api.xiaomimimo.com/v1",
        api_key_env_vars=("XIAOMI_API_KEY",),
        base_url_env_var="XIAOMI_BASE_URL",
    ),
    "tencent-tokenhub": ProviderConfig(
        id="tencent-tokenhub",
        name="Tencent TokenHub",
        auth_type="api_key",
        inference_base_url="https://tokenhub.tencentmaas.com/v1",
        api_key_env_vars=("TOKENHUB_API_KEY",),
        base_url_env_var="TOKENHUB_BASE_URL",
    ),
    "ollama-cloud": ProviderConfig(
        id="ollama-cloud",
        name="Ollama Cloud",
        auth_type="api_key",
        inference_base_url=DEFAULT_OLLAMA_CLOUD_BASE_URL,
        api_key_env_vars=("OLLAMA_API_KEY",),
        base_url_env_var="OLLAMA_BASE_URL",
    ),
    "bedrock": ProviderConfig(
        id="bedrock",
        name="AWS Bedrock",
        auth_type="aws_sdk",
        inference_base_url="https://bedrock-runtime.us-east-1.amazonaws.com",
        api_key_env_vars=(),
        base_url_env_var="BEDROCK_BASE_URL",
    ),
    "azure-foundry": ProviderConfig(
        id="azure-foundry",
        name="Azure Foundry",
        auth_type="api_key",
        inference_base_url="",  # User-provided endpoint
        api_key_env_vars=("AZURE_FOUNDRY_API_KEY",),
        base_url_env_var="AZURE_FOUNDRY_BASE_URL",
    ),
}

# Auto-extend PROVIDER_REGISTRY with any api-key provider registered in
# providers/ that is not already declared above.  New providers only need a
# plugins/model-providers/<name>/ plugin — no edits to this file required.
try:
    from providers import list_providers as _list_providers_for_registry
    for _pp in _list_providers_for_registry():
        if _pp.name in PROVIDER_REGISTRY:
            continue
        if _pp.auth_type != "api_key" or not _pp.env_vars:
            continue
        # Skip providers that need custom token resolution or are special-cased
        # in resolve_provider() (copilot/kimi/zai have bespoke token refresh;
        # openrouter/custom are aggregator/user-supplied and handled outside
        # the registry — adding them here breaks runtime_provider resolution
        # that relies on `openrouter not in PROVIDER_REGISTRY`).
        if _pp.name in {"copilot", "kimi-coding", "kimi-coding-cn", "zai", "openrouter", "custom"}:
            continue
        _api_key_vars = tuple(v for v in _pp.env_vars if not v.endswith("_BASE_URL") and not v.endswith("_URL"))
        _base_url_var = next((v for v in _pp.env_vars if v.endswith("_BASE_URL") or v.endswith("_URL")), None)
        PROVIDER_REGISTRY[_pp.name] = ProviderConfig(
            id=_pp.name,
            name=_pp.display_name or _pp.name,
            auth_type="api_key",
            inference_base_url=_pp.base_url,
            api_key_env_vars=_api_key_vars or _pp.env_vars,
            base_url_env_var=_base_url_var or "",
        )
        # Also register aliases so resolve_provider() resolves them
        for _alias in _pp.aliases:
            if _alias not in PROVIDER_REGISTRY:
                PROVIDER_REGISTRY[_alias] = PROVIDER_REGISTRY[_pp.name]
except Exception:
    pass


# =============================================================================
# Anthropic Key Helper
# =============================================================================

def get_anthropic_key() -> str:
    """Return the first usable Anthropic credential, or ``""``.

    Checks both the ``.env`` file (via ``get_env_value``) and the process
    environment (``os.getenv``).  The fallback order mirrors the
    ``PROVIDER_REGISTRY["anthropic"].api_key_env_vars`` tuple:

        ANTHROPIC_API_KEY -> ANTHROPIC_TOKEN -> CLAUDE_CODE_OAUTH_TOKEN
    """
    from janus_cli.config import get_env_value

    for var in PROVIDER_REGISTRY["anthropic"].api_key_env_vars:
        value = get_env_value(var) or os.getenv(var, "")
        if value:
            return value
    return ""


# =============================================================================
# Kimi Code Endpoint Detection
# =============================================================================

# Kimi Code (kimi.com/code) issues keys prefixed "sk-kimi-" that only work
# on api.kimi.com/coding.  Legacy keys from platform.moonshot.ai work on
# api.moonshot.ai/v1 (the old default).  Auto-detect when user hasn't set
# KIMI_BASE_URL explicitly.
#
# Note: the base URL intentionally has NO /v1 suffix.  The /coding endpoint
# speaks the Anthropic Messages protocol, and the anthropic SDK appends
# "/v1/messages" internally — so "/coding" + SDK suffix → "/coding/v1/messages"
# (the correct target). Using "/coding/v1" here would produce
# "/coding/v1/v1/messages" (a 404).
KIMI_CODE_BASE_URL = "https://api.kimi.com/coding"


def _resolve_kimi_base_url(api_key: str, default_url: str, env_override: str) -> str:
    """Return the correct Kimi base URL based on the API key prefix.

    If the user has explicitly set KIMI_BASE_URL, that always wins.
    Otherwise, sk-kimi- prefixed keys route to api.kimi.com/coding/v1.
    """
    if env_override:
        return env_override
    # No key → nothing to infer from.  Return default without inspecting.
    if not api_key:
        return default_url
    if api_key.startswith("sk-kimi-"):
        return KIMI_CODE_BASE_URL
    return default_url


_PLACEHOLDER_SECRET_VALUES = {
    "*",
    "**",
    "***",
    "changeme",
    "your_api_key",
    "your_api_key_here",
    "your-api-key",
    "placeholder",
    "example",
    "dummy",
    "null",
    "none",
}


def has_usable_secret(value: Any, *, min_length: int = 4) -> bool:
    """Return True when a configured secret looks usable, not empty/placeholder."""
    if not isinstance(value, str):
        return False
    cleaned = value.strip()
    if len(cleaned) < min_length:
        return False
    if cleaned.lower() in _PLACEHOLDER_SECRET_VALUES:
        return False
    return True


def _resolve_api_key_provider_secret(
    provider_id: str, pconfig: ProviderConfig
) -> tuple[str, str]:
    """Resolve an API-key provider's token and indicate where it came from."""
    if provider_id == "copilot":
        # Use the dedicated copilot auth module for proper token validation
        try:
            from janus_cli.copilot_auth import resolve_copilot_token, get_copilot_api_token
            token, source = resolve_copilot_token()
            if token:
                return get_copilot_api_token(token), source
        except ValueError as exc:
            logger.warning("Copilot token validation failed: %s", exc)
        except Exception:
            pass
        return "", ""

    from janus_cli.config import get_env_value
    for env_var in pconfig.api_key_env_vars:
        # Check both os.environ and ~/.janus/.env file
        val = (get_env_value(env_var) or "").strip()
        if has_usable_secret(val):
            return val, env_var

    # Fallback: try credential pool (e.g. zai key stored via auth.json)
    try:
        from agent.credential_pool import load_pool
        pool = load_pool(provider_id)
        if pool and pool.has_credentials():
            entry = pool.peek()
            if entry:
                key = getattr(entry, "access_token", "") or getattr(entry, "runtime_api_key", "")
                key = str(key).strip()
                if has_usable_secret(key):
                    return key, f"credential_pool:{provider_id}"
    except Exception:
        pass

    return "", ""


# =============================================================================
# Z.AI Endpoint Detection
# =============================================================================

# Z.AI has separate billing for general vs coding plans, and global vs China
# endpoints.  A key that works on one may return "Insufficient balance" on
# another.  We probe at setup time and store the working endpoint.
# Each entry lists candidate models to try in order — newer coding plan accounts
# may only have access to recent models (glm-5.1, glm-5v-turbo) while older
# ones still use glm-4.7.

ZAI_ENDPOINTS = [
    # (id, base_url, probe_models, label)
    ("global",        "https://api.z.ai/api/paas/v4",        ["glm-5"],   "Global"),
    ("cn",            "https://open.bigmodel.cn/api/paas/v4", ["glm-5"],   "China"),
    ("coding-global", "https://api.z.ai/api/coding/paas/v4",  ["glm-5.1", "glm-5v-turbo", "glm-4.7"], "Global (Coding Plan)"),
    ("coding-cn",     "https://open.bigmodel.cn/api/coding/paas/v4", ["glm-5.1", "glm-5v-turbo", "glm-4.7"], "China (Coding Plan)"),
]


def detect_zai_endpoint(api_key: str, timeout: float = 8.0) -> Optional[Dict[str, str]]:
    """Probe z.ai endpoints to find one that accepts this API key.

    Returns {"id": ..., "base_url": ..., "model": ..., "label": ...} for the
    first working endpoint, or None if all fail.  For endpoints with multiple
    candidate models, tries each in order and returns the first that succeeds.
    """
    for ep_id, base_url, probe_models, label in ZAI_ENDPOINTS:
        for model in probe_models:
            try:
                resp = httpx.post(
                    f"{base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "stream": False,
                        "max_tokens": 1,
                        "messages": [{"role": "user", "content": "ping"}],
                    },
                    timeout=timeout,
                )
                if resp.status_code == 200:
                    logger.debug("Z.AI endpoint probe: %s (%s) model=%s OK", ep_id, base_url, model)
                    return {
                        "id": ep_id,
                        "base_url": base_url,
                        "model": model,
                        "label": label,
                    }
                logger.debug("Z.AI endpoint probe: %s model=%s returned %s", ep_id, model, resp.status_code)
            except Exception as exc:
                logger.debug("Z.AI endpoint probe: %s model=%s failed: %s", ep_id, model, exc)
    return None


def _resolve_zai_base_url(api_key: str, default_url: str, env_override: str) -> str:
    """Return the correct Z.AI base URL by probing endpoints.

    If the user has explicitly set GLM_BASE_URL, that always wins.
    Otherwise, probe the candidate endpoints to find one that accepts the
    key.  The detected endpoint is cached in provider state (auth.json) keyed
    on a hash of the API key so subsequent starts skip the probe.
    """
    if env_override:
        return env_override

    # No API key set → don't probe (would fire N×M HTTPS requests with an
    # empty Bearer token, all returning 401).  This path is hit during
    # auxiliary-client auto-detection when the user has no Z.AI credentials
    # at all — the caller discards the result immediately, so the probe is
    # pure latency for every AIAgent construction.
    if not api_key:
        return default_url

    # Check provider-state cache for a previously-detected endpoint.
    auth_store = _load_auth_store()
    state = _load_provider_state(auth_store, "zai") or {}
    cached = state.get("detected_endpoint")
    if isinstance(cached, dict) and cached.get("base_url"):
        key_hash = cached.get("key_hash", "")
        if key_hash == hashlib.sha256(api_key.encode()).hexdigest()[:16]:
            logger.debug("Z.AI: using cached endpoint %s", cached["base_url"])
            return cached["base_url"]

    # Probe — may take up to ~8s per endpoint.
    detected = detect_zai_endpoint(api_key)
    if detected and detected.get("base_url"):
        # Persist the detection result keyed on the API key hash.
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:16]
        state["detected_endpoint"] = {
            "base_url": detected["base_url"],
            "endpoint_id": detected.get("id", ""),
            "model": detected.get("model", ""),
            "label": detected.get("label", ""),
            "key_hash": key_hash,
        }
        _save_provider_state(auth_store, "zai", state)
        logger.info("Z.AI: auto-detected endpoint %s (%s)", detected["label"], detected["base_url"])
        return detected["base_url"]

    logger.debug("Z.AI: probe failed, falling back to default %s", default_url)
    return default_url


# =============================================================================
# Error Types
# =============================================================================

# Error code marking upstream rate-limit / usage-quota exhaustion (HTTP 429).
# Such failures are transient and re-authenticating cannot resolve them, so
# they must be kept distinct from missing/expired-credential errors.
CODEX_RATE_LIMITED_CODE = "codex_rate_limited"


class AuthError(RuntimeError):
    """Structured auth error with UX mapping hints."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "",
        code: Optional[str] = None,
        relogin_required: bool = False,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.code = code
        self.relogin_required = relogin_required


def is_rate_limited_auth_error(error: Exception) -> bool:
    """True when an :class:`AuthError` represents upstream rate-limiting / quota
    exhaustion rather than missing or invalid credentials.

    These failures are transient — re-authenticating cannot resolve them — so
    callers should surface a "retry later" notice and prefer a fallback chain
    instead of prompting the operator to run ``janus auth``.
    """
    return (
        isinstance(error, AuthError)
        and not error.relogin_required
        and error.code == CODEX_RATE_LIMITED_CODE
    )


def _parse_retry_after_seconds(headers: Any) -> Optional[int]:
    """Best-effort parse of a ``Retry-After`` header into whole seconds.

    Supports the delta-seconds form (e.g. ``"120"``). HTTP-date forms and
    missing/unparseable values return ``None`` rather than guessing.
    """
    if headers is None:
        return None
    try:
        raw = headers.get("retry-after")
    except Exception:
        return None
    if raw is None:
        return None
    try:
        seconds = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None


def format_auth_error(error: Exception) -> str:
    """Map auth failures to concise user-facing guidance."""
    if not isinstance(error, AuthError):
        return str(error)

    # Rate-limit / quota errors are not credential problems — never append the
    # "re-authenticate" remediation, which would mislead the operator.
    if is_rate_limited_auth_error(error):
        return str(error)

    if error.relogin_required:
        return f"{error} Run `janus model` to re-authenticate."

    if error.code == "temporarily_unavailable":
        return f"{error} Please retry in a few seconds."

    return str(error)


def _token_fingerprint(token: Any) -> Optional[str]:
    """Return a short hash fingerprint for telemetry without leaking token bytes."""
    if not isinstance(token, str):
        return None
    cleaned = token.strip()
    if not cleaned:
        return None
    return hashlib.sha256(cleaned.encode("utf-8")).hexdigest()[:12]


# =============================================================================
# Auth Store — persistence layer for ~/.janus/auth.json
# =============================================================================

def _auth_file_path() -> Path:
    path = get_janus_home() / "auth.json"
    # Seat belt: if pytest is running and JANUS_HOME resolves to the real
    # user's auth store, refuse rather than silently corrupt it. This catches
    # tests that forgot to monkeypatch JANUS_HOME, tests invoked without the
    # hermetic conftest, or sandbox escapes via threads/subprocesses. In
    # production (no PYTEST_CURRENT_TEST) this is a single dict lookup.
    if os.environ.get("PYTEST_CURRENT_TEST"):
        real_home_auth = (Path.home() / ".janus" / "auth.json").resolve(strict=False)
        try:
            resolved = path.resolve(strict=False)
        except Exception:
            resolved = path
        if resolved == real_home_auth:
            raise RuntimeError(
                f"Refusing to touch real user auth store during test run: {path}. "
                "Set JANUS_HOME to a tmp_path in your test fixture, or run "
                "via scripts/run_tests.sh for hermetic CI-parity env."
            )
    return path


def _global_auth_file_path() -> Optional[Path]:
    """Return the global-root auth.json when the process is in profile mode.

    Returns ``None`` when the profile and global root resolve to the same
    directory (classic mode, or custom JANUS_HOME that is not a profile).
    Used by read-only fallback paths so providers authed at the root are
    visible to profile processes that haven't configured them locally.

    See issue #18594 follow-up (credential_pool shadowing).
    """
    try:
        from janus_constants import get_default_janus_root
        global_root = get_default_janus_root()
    except Exception:
        return None
    profile_home = get_janus_home()
    try:
        if profile_home.resolve(strict=False) == global_root.resolve(strict=False):
            return None
    except Exception:
        if profile_home == global_root:
            return None
    # No pytest seat belt here: this is a pure read-only path, and
    # ``_load_global_auth_store()`` wraps the read in a try/except so an
    # unreadable global file can never break the profile process.  The
    # write-side seat belt still lives on ``_auth_file_path()`` where it
    # belongs (that's what protects the real user's auth store from being
    # corrupted by a mis-configured test).
    return global_root / "auth.json"


def _load_global_auth_store() -> Dict[str, Any]:
    """Load the global-root auth store (read-only fallback).

    Returns an empty dict when no global fallback exists (classic mode,
    or the global auth.json is absent). Never raises on missing file.

    Seat belt: under pytest, refuses to read the real user's
    ``~/.janus/auth.json`` even when JANUS_HOME is set to a profile
    path. The hermetic conftest does not redirect ``HOME``, so
    ``get_default_janus_root()`` for a profile-shaped JANUS_HOME can
    still resolve to the real user's home on a dev machine. That would
    leak real credentials into tests. This guard uses the unmodified
    ``HOME`` env var (what ``os.path.expanduser('~')`` would resolve to),
    not ``Path.home()``, because ``Path.home`` is sometimes monkeypatched
    by fixtures that want to relocate the global root to a tmp path.
    """
    global_path = _global_auth_file_path()
    if global_path is None or not global_path.exists():
        return {}
    if os.environ.get("PYTEST_CURRENT_TEST"):
        real_home_env = os.environ.get("HOME", "")
        if real_home_env:
            real_root = Path(real_home_env) / ".janus" / "auth.json"
            try:
                if global_path.resolve(strict=False) == real_root.resolve(strict=False):
                    return {}
            except Exception:
                pass
    try:
        return _load_auth_store(global_path)
    except Exception:
        # A malformed global store must not break profile reads. The
        # profile's own auth store is still authoritative.
        return {}


def _auth_lock_path() -> Path:
    return _auth_file_path().with_suffix(".lock")


_auth_lock_holder = threading.local()


@contextmanager
def _file_lock(
    lock_path: Path,
    holder: threading.local,
    timeout_seconds: float,
    timeout_message: str,
):
    """Cross-process advisory flock helper.

    Reentrant per-thread via ``holder.depth``. Falls back to a depth-only
    guard when neither ``fcntl`` nor ``msvcrt`` is available (rare).
    Callers supply their own ``threading.local`` so independent locks
    (e.g. profile auth.json vs a provider-specific store) don't share reentrancy
    state — that would let one lock's reentrant acquisition silently skip
    the other's kernel-level flock.
    """
    if getattr(holder, "depth", 0) > 0:
        holder.depth += 1
        try:
            yield
        finally:
            holder.depth -= 1
        return

    lock_path.parent.mkdir(parents=True, exist_ok=True)

    if fcntl is None and msvcrt is None:
        holder.depth = 1
        try:
            yield
        finally:
            holder.depth = 0
        return

    # On Windows, msvcrt.locking needs the file to have content and the
    # file pointer at position 0. Ensure the lock file has at least 1 byte.
    if msvcrt and (not lock_path.exists() or lock_path.stat().st_size == 0):
        lock_path.write_text(" ", encoding="utf-8")

    with lock_path.open("r+" if msvcrt else "a+", encoding="utf-8") as lock_file:
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        while True:
            try:
                if fcntl:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                break
            except (BlockingIOError, OSError, PermissionError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(timeout_message)
                time.sleep(0.05)

        holder.depth = 1
        try:
            yield
        finally:
            holder.depth = 0
            if fcntl:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                except (OSError, IOError):
                    pass
            elif msvcrt:
                try:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
                except (OSError, IOError):
                    pass


@contextmanager
def _auth_store_lock(timeout_seconds: float = AUTH_LOCK_TIMEOUT_SECONDS):
    """Cross-process advisory lock for auth.json reads+writes.  Reentrant."""
    with _file_lock(
        _auth_lock_path(),
        _auth_lock_holder,
        timeout_seconds,
        "Timed out waiting for auth store lock",
    ):
        yield


def _load_auth_store(auth_file: Optional[Path] = None) -> Dict[str, Any]:
    auth_file = auth_file or _auth_file_path()
    if not auth_file.exists():
        return {"version": AUTH_STORE_VERSION, "providers": {}}

    try:
        raw = json.loads(auth_file.read_text())
    except Exception as exc:
        corrupt_path = auth_file.with_suffix(".json.corrupt")
        try:
            import shutil
            shutil.copy2(auth_file, corrupt_path)
        except Exception:
            pass
        logger.warning(
            "auth: failed to parse %s (%s) — starting with empty store. "
            "Corrupt file preserved at %s",
            auth_file, exc, corrupt_path,
        )
        return {"version": AUTH_STORE_VERSION, "providers": {}}

    if isinstance(raw, dict) and (
        isinstance(raw.get("providers"), dict)
        or isinstance(raw.get("credential_pool"), dict)
    ):
        raw.setdefault("providers", {})
        return raw

    return {"version": AUTH_STORE_VERSION, "providers": {}}


def _save_auth_store(auth_store: Dict[str, Any]) -> Path:
    auth_file = _auth_file_path()
    auth_file.parent.mkdir(parents=True, exist_ok=True)
    # Tighten parent dir to 0o700 so siblings can't traverse to creds.
    # No-op on Windows (POSIX mode bits not enforced); ignore failures.
    # secure_parent_dir refuses to chmod / or top-level dirs (#25821).
    secure_parent_dir(auth_file)
    auth_store["version"] = AUTH_STORE_VERSION
    auth_store["updated_at"] = datetime.now(timezone.utc).isoformat()
    payload = json.dumps(auth_store, indent=2) + "\n"
    tmp_path = auth_file.with_name(f"{auth_file.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    try:
        # Create with 0o600 atomically via os.open(O_EXCL) + fdopen to close
        # the TOCTOU window where default umask (often 0o644) briefly exposed
        # OAuth tokens to other local users between open() and chmod().
        # Mirrors agent/google_oauth.py (#19673) and tools/mcp_oauth.py (#21148).
        fd = os.open(
            str(tmp_path),
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        atomic_replace(tmp_path, auth_file)
        try:
            dir_fd = os.open(str(auth_file.parent), os.O_RDONLY)
        except OSError:
            dir_fd = None
        if dir_fd is not None:
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
    # Restrict file permissions to owner only
    try:
        auth_file.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass
    return auth_file


def _load_provider_state(auth_store: Dict[str, Any], provider_id: str) -> Optional[Dict[str, Any]]:
    """Return a provider's persisted state.

    In profile mode, falls back to the global-root ``auth.json`` when the
    profile has no entry for ``provider_id``. This mirrors the per-provider
    shadowing already used by ``read_credential_pool``: workers spawned in a
    profile can see providers (e.g. ``openai-codex``) that were only authenticated at
    global scope. Once the user runs ``janus auth login <provider>`` inside
    the profile, the profile state fully shadows the global state on the next
    read. See issue #18594 follow-up.
    """
    providers = auth_store.get("providers")
    if isinstance(providers, dict):
        state = providers.get(provider_id)
        if isinstance(state, dict):
            return dict(state)

    # Read-only fallback to the global-root auth store (profile mode only;
    # returns empty dict in classic mode so this is a no-op).
    global_store = _load_global_auth_store()
    if global_store:
        global_providers = global_store.get("providers")
        if isinstance(global_providers, dict):
            global_state = global_providers.get(provider_id)
            if isinstance(global_state, dict):
                return dict(global_state)
    return None


def _save_provider_state(auth_store: Dict[str, Any], provider_id: str, state: Dict[str, Any]) -> None:
    providers = auth_store.setdefault("providers", {})
    if not isinstance(providers, dict):
        auth_store["providers"] = {}
        providers = auth_store["providers"]
    providers[provider_id] = state
    auth_store["active_provider"] = provider_id


def _store_provider_state(
    auth_store: Dict[str, Any],
    provider_id: str,
    state: Dict[str, Any],
    *,
    set_active: bool = True,
) -> None:
    providers = auth_store.setdefault("providers", {})
    if not isinstance(providers, dict):
        auth_store["providers"] = {}
        providers = auth_store["providers"]
    providers[provider_id] = state
    if set_active:
        auth_store["active_provider"] = provider_id


def is_known_auth_provider(provider_id: str) -> bool:
    normalized = (provider_id or "").strip().lower()
    return normalized in PROVIDER_REGISTRY or normalized in SERVICE_PROVIDER_NAMES


def get_auth_provider_display_name(provider_id: str) -> str:
    normalized = (provider_id or "").strip().lower()
    if normalized in PROVIDER_REGISTRY:
        return PROVIDER_REGISTRY[normalized].name
    return SERVICE_PROVIDER_NAMES.get(normalized, provider_id)


def read_credential_pool(provider_id: Optional[str] = None) -> Dict[str, Any]:
    """Return the persisted credential pool, or one provider slice.

    In profile mode, the profile's credential pool is authoritative. If a
    provider has no entries in the profile, entries from the global-root
    ``auth.json`` are used as a read-only fallback — so workers spawned in a
    profile can see providers that were only authenticated at global scope.

    Profile entries always win: the global fallback only applies per-provider
    when the profile has zero entries for that provider. Once the user runs
    ``janus auth add <provider>`` inside the profile, profile entries
    fully shadow global for that provider on the next read.

    Writes always go to the profile (``write_credential_pool`` is unchanged).
    See issue #18594 follow-up.
    """
    auth_store = _load_auth_store()
    pool = auth_store.get("credential_pool")
    if not isinstance(pool, dict):
        pool = {}

    global_pool: Dict[str, Any] = {}
    global_store = _load_global_auth_store()
    maybe_global_pool = global_store.get("credential_pool") if global_store else None
    if isinstance(maybe_global_pool, dict):
        global_pool = maybe_global_pool

    if provider_id is None:
        merged = dict(pool)
        for gp_key, gp_entries in global_pool.items():
            if not isinstance(gp_entries, list) or not gp_entries:
                continue
            # Per-provider shadowing: profile wins whenever it has ANY entries.
            existing = merged.get(gp_key)
            if isinstance(existing, list) and existing:
                continue
            merged[gp_key] = list(gp_entries)
        return merged

    provider_entries = pool.get(provider_id)
    if isinstance(provider_entries, list) and provider_entries:
        return list(provider_entries)
    # Profile has no entries for this provider — fall back to global.
    global_entries = global_pool.get(provider_id)
    return list(global_entries) if isinstance(global_entries, list) else []


def write_credential_pool(provider_id: str, entries: List[Dict[str, Any]]) -> Path:
    """Persist one provider's credential pool under auth.json.

    This is the final disk-boundary guard for borrowed/reference-only
    credentials. Callers may pass raw dictionaries, so sanitize here even when
    ``PooledCredential.to_dict()`` already did the same work upstream.
    """
    with _auth_store_lock():
        auth_store = _load_auth_store()
        pool = auth_store.get("credential_pool")
        if not isinstance(pool, dict):
            pool = {}
            auth_store["credential_pool"] = pool
        pool[provider_id] = [
            sanitize_borrowed_credential_payload(entry, provider_id)
            if isinstance(entry, dict) else entry
            for entry in entries
        ]
        return _save_auth_store(auth_store)


def suppress_credential_source(provider_id: str, source: str) -> None:
    """Mark a credential source as suppressed so it won't be re-seeded."""
    with _auth_store_lock():
        auth_store = _load_auth_store()
        suppressed = auth_store.setdefault("suppressed_sources", {})
        provider_list = suppressed.setdefault(provider_id, [])
        if source not in provider_list:
            provider_list.append(source)
        _save_auth_store(auth_store)


def is_source_suppressed(provider_id: str, source: str) -> bool:
    """Check if a credential source has been suppressed by the user."""
    try:
        auth_store = _load_auth_store()
        suppressed = auth_store.get("suppressed_sources", {})
        return source in suppressed.get(provider_id, [])
    except Exception:
        return False


def unsuppress_credential_source(provider_id: str, source: str) -> bool:
    """Clear a suppression marker so the source will be re-seeded on the next load.

    Returns True if a marker was cleared, False if no marker existed.
    """
    with _auth_store_lock():
        auth_store = _load_auth_store()
        suppressed = auth_store.get("suppressed_sources")
        if not isinstance(suppressed, dict):
            return False
        provider_list = suppressed.get(provider_id)
        if not isinstance(provider_list, list) or source not in provider_list:
            return False
        provider_list.remove(source)
        if not provider_list:
            suppressed.pop(provider_id, None)
        if not suppressed:
            auth_store.pop("suppressed_sources", None)
        _save_auth_store(auth_store)
        return True


def get_provider_auth_state(provider_id: str) -> Optional[Dict[str, Any]]:
    """Return persisted auth state for a provider, or None.

    In profile mode, ``_load_provider_state`` already falls back to the
    global-root ``auth.json`` per-provider when the profile has no entry —
    so this is now a thin convenience wrapper. Profile state always wins
    when present. Writes (``_save_auth_store`` / ``persist_*_credentials``)
    are unchanged — they still target the profile only. This mirrors
    ``read_credential_pool``'s per-provider shadowing semantics so that
    ``_seed_from_singletons`` can reseed a profile's credential pool from
    global-scope provider state (e.g. a globally-authenticated Anthropic
    OAuth or Codex OAuth session). See issue #18594 follow-up.
    """
    auth_store = _load_auth_store()
    return _load_provider_state(auth_store, provider_id)


def get_active_provider() -> Optional[str]:
    """Return the currently active provider ID from auth store."""
    auth_store = _load_auth_store()
    return auth_store.get("active_provider")


def is_provider_explicitly_configured(provider_id: str) -> bool:
    """Return True only if the user has explicitly configured this provider.

    Checks:
      1. active_provider in auth.json matches
      2. model.provider in config.yaml matches
      3. Provider-specific env vars are set (e.g. ANTHROPIC_API_KEY)

    This is used to gate auto-discovery of external credentials (e.g.
    Claude Code's ~/.claude/.credentials.json) so they are never used
    without the user's explicit choice.  See PR #4210 for the same
    pattern applied to the setup wizard gate.
    """
    normalized = (provider_id or "").strip().lower()

    # 1. Check auth.json active_provider
    try:
        auth_store = _load_auth_store()
        active = (auth_store.get("active_provider") or "").strip().lower()
        if active and active == normalized:
            return True
    except Exception:
        pass

    # 2. Check config.yaml model.provider
    try:
        from janus_cli.config import load_config
        cfg = load_config()
        model_cfg = cfg.get("model")
        if isinstance(model_cfg, dict):
            cfg_provider = (model_cfg.get("provider") or "").strip().lower()
            if cfg_provider == normalized:
                return True
    except Exception:
        pass

    # 3. Check provider-specific env vars
    # Exclude CLAUDE_CODE_OAUTH_TOKEN — it's set by Claude Code itself,
    # not by the user explicitly configuring anthropic in Janus.
    _IMPLICIT_ENV_VARS = {"CLAUDE_CODE_OAUTH_TOKEN"}
    pconfig = PROVIDER_REGISTRY.get(normalized)
    if pconfig and pconfig.auth_type == "api_key":
        for env_var in pconfig.api_key_env_vars:
            if env_var in _IMPLICIT_ENV_VARS:
                continue
            if has_usable_secret(os.getenv(env_var, "")):
                return True

    return False


def clear_provider_auth(provider_id: Optional[str] = None) -> bool:
    """
    Clear auth state for a provider. Used by `janus logout`.
    If provider_id is None, clears the active provider.
    Returns True if something was cleared.
    """
    with _auth_store_lock():
        auth_store = _load_auth_store()
        target = provider_id or auth_store.get("active_provider")
        if not target:
            return False

        providers = auth_store.get("providers", {})
        if not isinstance(providers, dict):
            providers = {}
            auth_store["providers"] = providers

        pool = auth_store.get("credential_pool")
        if not isinstance(pool, dict):
            pool = {}
            auth_store["credential_pool"] = pool

        cleared = False
        if target in providers:
            del providers[target]
            cleared = True
        if target in pool:
            del pool[target]
            cleared = True

        if auth_store.get("active_provider") == target:
            auth_store["active_provider"] = None
            cleared = True

        if not cleared:
            return False
        _save_auth_store(auth_store)
    return True


def deactivate_provider() -> None:
    """
    Clear active_provider in auth.json without deleting credentials.
    Used when the user switches to a non-OAuth provider (OpenRouter, custom)
    so auto-resolution doesn't keep picking the OAuth provider.
    """
    with _auth_store_lock():
        auth_store = _load_auth_store()
        auth_store["active_provider"] = None
        _save_auth_store(auth_store)


# =============================================================================
# Provider Resolution — picks which provider to use
# =============================================================================


def _get_config_hint_for_unknown_provider(provider_name: str) -> str:
    """Return a helpful hint string when provider resolution fails.

    Checks for common config.yaml mistakes (malformed custom_providers, etc.)
    and returns a human-readable diagnostic, or empty string if nothing found.
    """
    try:
        from janus_cli.config import validate_config_structure
        issues = validate_config_structure()
        if not issues:
            return ""

        lines = ["Config issue detected — run 'janus doctor' for full diagnostics:"]
        for ci in issues:
            prefix = "ERROR" if ci.severity == "error" else "WARNING"
            lines.append(f"  [{prefix}] {ci.message}")
            # Show first line of hint
            first_hint = ci.hint.splitlines()[0] if ci.hint else ""
            if first_hint:
                lines.append(f"    → {first_hint}")
        return "\n".join(lines)
    except Exception:
        return ""


def resolve_provider(
    requested: Optional[str] = None,
    *,
    explicit_api_key: Optional[str] = None,
    explicit_base_url: Optional[str] = None,
) -> str:
    """
    Determine which inference provider to use.

    Priority (when requested="auto" or None):
    1. active_provider in auth.json with valid credentials
    2. Explicit CLI api_key/base_url -> "openrouter"
    3. OPENAI_API_KEY or OPENROUTER_API_KEY env vars -> "openrouter"
    4. Provider-specific API keys (GLM, Kimi, MiniMax) -> that provider
    5. Fallback: "openrouter"
    """
    normalized = (requested or "auto").strip().lower()

    # Normalize provider aliases
    _PROVIDER_ALIASES = {
        "glm": "zai", "z-ai": "zai", "z.ai": "zai", "zhipu": "zai",
        "google": "gemini", "google-gemini": "gemini", "google-ai-studio": "gemini",
        "x-ai": "xai", "x.ai": "xai", "grok": "xai",
        "xai-oauth": "xai-oauth", "x-ai-oauth": "xai-oauth",
        "grok-oauth": "xai-oauth", "xai-grok-oauth": "xai-oauth",
        "kimi": "kimi-coding", "kimi-for-coding": "kimi-coding", "moonshot": "kimi-coding",
        "kimi-cn": "kimi-coding-cn", "moonshot-cn": "kimi-coding-cn",
        "step": "stepfun", "stepfun-coding-plan": "stepfun",
        "arcee-ai": "arcee", "arceeai": "arcee",
        "gmi-cloud": "gmi", "gmicloud": "gmi",
        "minimax-china": "minimax-cn", "minimax_cn": "minimax-cn",
        "minimax-portal": "minimax-oauth", "minimax-global": "minimax-oauth", "minimax_oauth": "minimax-oauth",
        "alibaba_coding": "alibaba-coding-plan", "alibaba-coding": "alibaba-coding-plan",
        "alibaba_coding_plan": "alibaba-coding-plan",
        "claude": "anthropic", "claude-code": "anthropic",
        "github": "copilot", "github-copilot": "copilot",
        "github-models": "copilot", "github-model": "copilot",
        "github-copilot-acp": "copilot-acp", "copilot-acp-agent": "copilot-acp",
        "opencode": "opencode-zen", "zen": "opencode-zen",
        "qwen-portal": "qwen-oauth", "qwen-cli": "qwen-oauth", "qwen-oauth": "qwen-oauth", "google-gemini-cli": "google-gemini-cli", "gemini-cli": "google-gemini-cli", "gemini-oauth": "google-gemini-cli",
        "hf": "huggingface", "hugging-face": "huggingface", "huggingface-hub": "huggingface",
        "mimo": "xiaomi", "xiaomi-mimo": "xiaomi",
        "tencent": "tencent-tokenhub", "tokenhub": "tencent-tokenhub",
        "tencent-cloud": "tencent-tokenhub", "tencentmaas": "tencent-tokenhub",
        "aws": "bedrock", "aws-bedrock": "bedrock", "amazon-bedrock": "bedrock", "amazon": "bedrock",
        "go": "opencode-go", "opencode-go-sub": "opencode-go",
        "kilo": "kilocode", "kilo-code": "kilocode", "kilo-gateway": "kilocode",
        "lmstudio": "lmstudio", "lm-studio": "lmstudio", "lm_studio": "lmstudio",
        # Local server aliases — route through the generic custom provider
        "ollama": "custom", "ollama_cloud": "ollama-cloud",
        "vllm": "custom", "llamacpp": "custom",
        "llama.cpp": "custom", "llama-cpp": "custom",
    }
    # Extend with aliases declared in plugins/model-providers/<name>/ that aren't already mapped.
    # This keeps providers/ as the single source for new aliases while the
    # hardcoded dict above remains authoritative for existing ones.
    try:
        from providers import list_providers as _lp
        for _pp in _lp():
            for _alias in _pp.aliases:
                if _alias not in _PROVIDER_ALIASES:
                    _PROVIDER_ALIASES[_alias] = _pp.name
    except Exception:
        pass
    normalized = _PROVIDER_ALIASES.get(normalized, normalized)

    if normalized == "openrouter":
        return "openrouter"
    if normalized == "custom":
        return "custom"
    if normalized in PROVIDER_REGISTRY:
        return normalized
    if normalized != "auto":
        # Check for common config.yaml issues that cause this error
        _config_hint = _get_config_hint_for_unknown_provider(normalized)
        msg = f"Unknown provider '{normalized}'."
        if _config_hint:
            msg += f"\n\n{_config_hint}"
        else:
            msg += " Check 'janus model' for available providers, or run 'janus doctor' to diagnose config issues."
        raise AuthError(msg, code="invalid_provider")

    # Explicit one-off CLI creds always mean openrouter/custom
    if explicit_api_key or explicit_base_url:
        return "openrouter"

    # Check auth store for an active OAuth provider
    try:
        auth_store = _load_auth_store()
        active = auth_store.get("active_provider")
        if active and active in PROVIDER_REGISTRY:
            status = get_auth_status(active)
            if status.get("logged_in"):
                return active
    except Exception as e:
        logger.debug("Could not detect active auth provider: %s", e)

    if has_usable_secret(os.getenv("OPENAI_API_KEY")) or has_usable_secret(os.getenv("OPENROUTER_API_KEY")):
        return "openrouter"

    # Auto-detect API-key providers by checking their env vars
    for pid, pconfig in PROVIDER_REGISTRY.items():
        if pconfig.auth_type != "api_key":
            continue
        # GitHub tokens are commonly present for repo/tool access but should not
        # hijack inference auto-selection unless the user explicitly chooses
        # Copilot/GitHub Models as the provider. LM Studio is a local server
        # whose availability isn't implied by LM_API_KEY presence (it may be
        # offline, and the no-auth setup uses a placeholder value), so it
        # also requires explicit selection.
        if pid in {"copilot", "lmstudio"}:
            continue
        for env_var in pconfig.api_key_env_vars:
            if has_usable_secret(os.getenv(env_var, "")):
                return pid

    # AWS Bedrock — detect via boto3 credential chain (IAM roles, SSO, env vars).
    # This runs after API-key providers so explicit keys always win.
    try:
        from agent.bedrock_adapter import has_aws_credentials
        if has_aws_credentials():
            return "bedrock"
    except ImportError:
        pass  # boto3 not installed — skip Bedrock auto-detection

    raise AuthError(
        "No inference provider configured. Run 'janus model' to choose a "
        "provider and model, or set an API key (OPENROUTER_API_KEY, "
        "OPENAI_API_KEY, etc.) in ~/.janus/.env.",
        code="no_provider_configured",
    )


# =============================================================================
# Timestamp / TTL helpers
# =============================================================================

def _parse_iso_timestamp(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _is_expiring(expires_at_iso: Any, skew_seconds: int) -> bool:
    expires_epoch = _parse_iso_timestamp(expires_at_iso)
    if expires_epoch is None:
        return True
    return expires_epoch <= (time.time() + skew_seconds)


def _coerce_ttl_seconds(expires_in: Any) -> int:
    try:
        ttl = int(expires_in)
    except Exception:
        ttl = 0
    return max(0, ttl)


def _decode_jwt_claims(token: Any) -> Dict[str, Any]:
    if not isinstance(token, str) or token.count(".") != 2:
        return {}
    payload = token.split(".")[1]
    payload += "=" * ((4 - len(payload) % 4) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload.encode("utf-8"))
        claims = json.loads(raw.decode("utf-8"))
    except Exception:
        return {}
    return claims if isinstance(claims, dict) else {}


def _codex_access_token_is_expiring(access_token: Any, skew_seconds: int) -> bool:
    claims = _decode_jwt_claims(access_token)
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)):
        return False
    return float(exp) <= (time.time() + max(0, int(skew_seconds)))


def _qwen_cli_auth_path() -> Path:
    return Path.home() / ".qwen" / "oauth_creds.json"


def _read_qwen_cli_tokens() -> Dict[str, Any]:
    auth_path = _qwen_cli_auth_path()
    if not auth_path.exists():
        raise AuthError(
            "Qwen CLI credentials not found. Run 'qwen auth qwen-oauth' first.",
            provider="qwen-oauth",
            code="qwen_auth_missing",
        )
    try:
        data = json.loads(auth_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AuthError(
            f"Failed to read Qwen CLI credentials from {auth_path}: {exc}",
            provider="qwen-oauth",
            code="qwen_auth_read_failed",
        ) from exc
    if not isinstance(data, dict):
        raise AuthError(
            f"Invalid Qwen CLI credentials in {auth_path}.",
            provider="qwen-oauth",
            code="qwen_auth_invalid",
        )
    return data


def _save_qwen_cli_tokens(tokens: Dict[str, Any]) -> Path:
    auth_path = _qwen_cli_auth_path()
    auth_path.parent.mkdir(parents=True, exist_ok=True)
    # secure_parent_dir refuses to chmod / or top-level dirs (#25821).
    secure_parent_dir(auth_path)
    # Per-process random temp suffix avoids collisions between concurrent
    # writers and stale leftovers from a crashed prior write.
    tmp_path = auth_path.with_name(f"{auth_path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    # Create with 0o600 atomically via os.open(O_EXCL) — closes the TOCTOU
    # window where write_text() + post-write chmod briefly exposed tokens
    # at process umask (typically 0o644). See #19673, #21148.
    fd = os.open(
        str(tmp_path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        stat.S_IRUSR | stat.S_IWUSR,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(tokens, indent=2, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        atomic_replace(tmp_path, auth_path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
    return auth_path


def _qwen_access_token_is_expiring(expiry_date_ms: Any, skew_seconds: int = QWEN_ACCESS_TOKEN_REFRESH_SKEW_SECONDS) -> bool:
    try:
        expiry_ms = int(expiry_date_ms)
    except Exception:
        return True
    return (time.time() + max(0, int(skew_seconds))) * 1000 >= expiry_ms


def _refresh_qwen_cli_tokens(tokens: Dict[str, Any], timeout_seconds: float = 20.0) -> Dict[str, Any]:
    refresh_token = str(tokens.get("refresh_token", "") or "").strip()
    if not refresh_token:
        raise AuthError(
            "Qwen OAuth refresh token missing. Re-run 'qwen auth qwen-oauth'.",
            provider="qwen-oauth",
            code="qwen_refresh_token_missing",
        )

    try:
        response = httpx.post(
            QWEN_OAUTH_TOKEN_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": QWEN_OAUTH_CLIENT_ID,
            },
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise AuthError(
            f"Qwen OAuth refresh failed: {exc}",
            provider="qwen-oauth",
            code="qwen_refresh_failed",
        ) from exc

    if response.status_code >= 400:
        body = response.text.strip()
        raise AuthError(
            "Qwen OAuth refresh failed. Re-run 'qwen auth qwen-oauth'."
            + (f" Response: {body}" if body else ""),
            provider="qwen-oauth",
            code="qwen_refresh_failed",
        )

    try:
        payload = response.json()
    except Exception as exc:
        raise AuthError(
            f"Qwen OAuth refresh returned invalid JSON: {exc}",
            provider="qwen-oauth",
            code="qwen_refresh_invalid_json",
        ) from exc

    if not isinstance(payload, dict) or not str(payload.get("access_token", "") or "").strip():
        raise AuthError(
            "Qwen OAuth refresh response missing access_token.",
            provider="qwen-oauth",
            code="qwen_refresh_invalid_response",
        )

    expires_in = payload.get("expires_in")
    try:
        expires_in_seconds = int(expires_in)
    except Exception:
        expires_in_seconds = 6 * 60 * 60

    refreshed = {
        "access_token": str(payload.get("access_token", "") or "").strip(),
        "refresh_token": str(payload.get("refresh_token", refresh_token) or refresh_token).strip(),
        "token_type": str(payload.get("token_type", tokens.get("token_type", "Bearer")) or "Bearer").strip() or "Bearer",
        "resource_url": str(payload.get("resource_url", tokens.get("resource_url", "portal.qwen.ai")) or "portal.qwen.ai").strip(),
        "expiry_date": int(time.time() * 1000) + max(1, expires_in_seconds) * 1000,
    }
    _save_qwen_cli_tokens(refreshed)
    return refreshed


def _mark_qwen_oauth_active(creds: Dict[str, Any]) -> None:
    """Set active_provider to qwen-oauth in auth.json.

    Qwen OAuth tokens live in the Qwen CLI credential file managed by
    _save_qwen_cli_tokens / resolve_qwen_runtime_credentials. This function
    only writes a minimal provider-state entry (base_url for display) and
    sets active_provider so that get_active_provider() and
    _model_section_has_credentials() detect the provider for the setup wizard
    and status commands.
    """
    with _auth_store_lock():
        auth_store = _load_auth_store()
        state: Dict[str, Any] = {}
        if creds.get("base_url"):
            state["base_url"] = str(creds["base_url"])
        _save_provider_state(auth_store, "qwen-oauth", state)
        _save_auth_store(auth_store)


def resolve_qwen_runtime_credentials(
    *,
    force_refresh: bool = False,
    refresh_if_expiring: bool = True,
    refresh_skew_seconds: int = QWEN_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
) -> Dict[str, Any]:
    tokens = _read_qwen_cli_tokens()
    access_token = str(tokens.get("access_token", "") or "").strip()
    should_refresh = bool(force_refresh)
    if not should_refresh and refresh_if_expiring:
        should_refresh = _qwen_access_token_is_expiring(tokens.get("expiry_date"), refresh_skew_seconds)
    if should_refresh:
        tokens = _refresh_qwen_cli_tokens(tokens)
        access_token = str(tokens.get("access_token", "") or "").strip()
    if not access_token:
        raise AuthError(
            "Qwen OAuth access token missing. Re-run 'qwen auth qwen-oauth'.",
            provider="qwen-oauth",
            code="qwen_access_token_missing",
        )

    base_url = os.getenv("JANUS_QWEN_BASE_URL", "").strip().rstrip("/") or DEFAULT_QWEN_BASE_URL
    return {
        "provider": "qwen-oauth",
        "base_url": base_url,
        "api_key": access_token,
        "source": "qwen-cli",
        "expires_at_ms": tokens.get("expiry_date"),
        "auth_file": str(_qwen_cli_auth_path()),
    }


def get_qwen_auth_status() -> Dict[str, Any]:
    auth_path = _qwen_cli_auth_path()
    try:
        # Validate the runtime credentials, including refresh when the cached
        # CLI token is expired. Otherwise stale tokens show up as "logged in"
        # and `janus model` walks users into a broken Qwen setup flow.
        creds = resolve_qwen_runtime_credentials(refresh_if_expiring=True)
        return {
            "logged_in": True,
            "auth_file": str(auth_path),
            "source": creds.get("source"),
            "api_key": creds.get("api_key"),
            "expires_at_ms": creds.get("expires_at_ms"),
        }
    except AuthError as exc:
        return {
            "logged_in": False,
            "auth_file": str(auth_path),
            "error": str(exc),
        }


# =============================================================================
# Google Gemini OAuth (google-gemini-cli) — PKCE flow + Cloud Code Assist.
#
# Tokens live in ~/.janus/auth/google_oauth.json (managed by agent.google_oauth).
# The `base_url` here is the marker "cloudcode-pa://google" that run_agent.py
# uses to construct a GeminiCloudCodeClient instead of the default OpenAI SDK.
# Actual HTTP traffic goes to https://cloudcode-pa.googleapis.com/v1internal:*.
# =============================================================================

def _mark_google_gemini_cli_active(creds: Dict[str, Any]) -> None:
    """Set active_provider to google-gemini-cli in auth.json.

    The actual OAuth tokens live in the Google credential file managed by
    agent.google_oauth. This function only writes a minimal provider-state
    entry (email for display) and sets active_provider so that
    get_active_provider() and _model_section_has_credentials() detect the
    provider for the setup wizard and status commands.
    """
    with _auth_store_lock():
        auth_store = _load_auth_store()
        state: Dict[str, Any] = {}
        if creds.get("email"):
            state["email"] = str(creds["email"])
        _save_provider_state(auth_store, "google-gemini-cli", state)
        _save_auth_store(auth_store)


def resolve_gemini_oauth_runtime_credentials(
    *,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Resolve runtime OAuth creds for google-gemini-cli."""
    try:
        from agent.google_oauth import (
            GoogleOAuthError,
            _credentials_path,
            get_valid_access_token,
            load_credentials,
        )
    except ImportError as exc:
        raise AuthError(
            f"agent.google_oauth is not importable: {exc}",
            provider="google-gemini-cli",
            code="google_oauth_module_missing",
        ) from exc

    try:
        access_token = get_valid_access_token(force_refresh=force_refresh)
    except GoogleOAuthError as exc:
        raise AuthError(
            str(exc),
            provider="google-gemini-cli",
            code=exc.code,
        ) from exc

    creds = load_credentials()
    base_url = DEFAULT_GEMINI_CLOUDCODE_BASE_URL
    return {
        "provider": "google-gemini-cli",
        "base_url": base_url,
        "api_key": access_token,
        "source": "google-oauth",
        "expires_at_ms": (creds.expires_ms if creds else None),
        "auth_file": str(_credentials_path()),
        "email": (creds.email if creds else "") or "",
        "project_id": (creds.project_id if creds else "") or "",
    }


def get_gemini_oauth_auth_status() -> Dict[str, Any]:
    """Return a status dict for `janus auth list` / `janus status`."""
    try:
        from agent.google_oauth import _credentials_path, load_credentials
    except ImportError:
        return {"logged_in": False, "error": "agent.google_oauth unavailable"}
    auth_path = _credentials_path()
    creds = load_credentials()
    if creds is None or not creds.access_token:
        return {
            "logged_in": False,
            "auth_file": str(auth_path),
            "error": "not logged in",
        }
    return {
        "logged_in": True,
        "auth_file": str(auth_path),
        "source": "google-oauth",
        "api_key": creds.access_token,
        "expires_at_ms": creds.expires_ms,
        "email": creds.email,
        "project_id": creds.project_id,
    }
# Spotify auth — PKCE tokens stored in ~/.janus/auth.json
# =============================================================================


def _spotify_scope_list(raw_scope: Optional[str] = None) -> List[str]:
    scope_text = (raw_scope or DEFAULT_SPOTIFY_SCOPE).strip()
    scopes = [part for part in scope_text.split() if part]
    seen: set[str] = set()
    ordered: List[str] = []
    for scope in scopes:
        if scope not in seen:
            seen.add(scope)
            ordered.append(scope)
    return ordered


def _spotify_scope_string(raw_scope: Optional[str] = None) -> str:
    return " ".join(_spotify_scope_list(raw_scope))


def _spotify_client_id(
    explicit: Optional[str] = None,
    state: Optional[Dict[str, Any]] = None,
) -> str:
    from janus_cli.config import get_env_value

    candidates = (
        explicit,
        get_env_value("JANUS_SPOTIFY_CLIENT_ID"),
        get_env_value("SPOTIFY_CLIENT_ID"),
        state.get("client_id") if isinstance(state, dict) else None,
    )
    for candidate in candidates:
        cleaned = str(candidate or "").strip()
        if cleaned:
            return cleaned
    raise AuthError(
        "Spotify client_id is required. Set JANUS_SPOTIFY_CLIENT_ID or pass --client-id.",
        provider="spotify",
        code="spotify_client_id_missing",
    )


def _spotify_redirect_uri(
    explicit: Optional[str] = None,
    state: Optional[Dict[str, Any]] = None,
) -> str:
    from janus_cli.config import get_env_value

    candidates = (
        explicit,
        get_env_value("JANUS_SPOTIFY_REDIRECT_URI"),
        get_env_value("SPOTIFY_REDIRECT_URI"),
        state.get("redirect_uri") if isinstance(state, dict) else None,
        DEFAULT_SPOTIFY_REDIRECT_URI,
    )
    for candidate in candidates:
        cleaned = str(candidate or "").strip()
        if cleaned:
            return cleaned
    return DEFAULT_SPOTIFY_REDIRECT_URI


def _spotify_api_base_url(state: Optional[Dict[str, Any]] = None) -> str:
    from janus_cli.config import get_env_value

    candidates = (
        get_env_value("JANUS_SPOTIFY_API_BASE_URL"),
        state.get("api_base_url") if isinstance(state, dict) else None,
        DEFAULT_SPOTIFY_API_BASE_URL,
    )
    for candidate in candidates:
        cleaned = str(candidate or "").strip().rstrip("/")
        if cleaned:
            return cleaned
    return DEFAULT_SPOTIFY_API_BASE_URL


def _spotify_accounts_base_url(state: Optional[Dict[str, Any]] = None) -> str:
    from janus_cli.config import get_env_value

    candidates = (
        get_env_value("JANUS_SPOTIFY_ACCOUNTS_BASE_URL"),
        state.get("accounts_base_url") if isinstance(state, dict) else None,
        DEFAULT_SPOTIFY_ACCOUNTS_BASE_URL,
    )
    for candidate in candidates:
        cleaned = str(candidate or "").strip().rstrip("/")
        if cleaned:
            return cleaned
    return DEFAULT_SPOTIFY_ACCOUNTS_BASE_URL


def _spotify_code_verifier(length: int = 64) -> str:
    raw = base64.urlsafe_b64encode(os.urandom(length)).decode("ascii")
    return raw.rstrip("=")[:128]


def _spotify_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _oauth_pkce_code_verifier(length: int = 64) -> str:
    raw = base64.urlsafe_b64encode(os.urandom(length)).decode("ascii")
    return raw.rstrip("=")[:128]


def _oauth_pkce_code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _spotify_build_authorize_url(
    *,
    client_id: str,
    redirect_uri: str,
    scope: str,
    state: str,
    code_challenge: str,
    accounts_base_url: str,
) -> str:
    query = urlencode({
        "client_id": client_id,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge_method": "S256",
        "code_challenge": code_challenge,
    })
    return f"{accounts_base_url}/authorize?{query}"


def _spotify_validate_redirect_uri(redirect_uri: str) -> tuple[str, int, str]:
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http":
        raise AuthError(
            "Spotify PKCE redirect_uri must use http://localhost or http://127.0.0.1.",
            provider="spotify",
            code="spotify_redirect_invalid",
        )
    host = parsed.hostname or ""
    if host not in {"127.0.0.1", "localhost"}:
        raise AuthError(
            "Spotify PKCE redirect_uri must point to localhost or 127.0.0.1.",
            provider="spotify",
            code="spotify_redirect_invalid",
        )
    if not parsed.port:
        raise AuthError(
            "Spotify PKCE redirect_uri must include an explicit localhost port.",
            provider="spotify",
            code="spotify_redirect_invalid",
        )
    return host, parsed.port, parsed.path or "/"


def _make_spotify_callback_handler(expected_path: str) -> tuple[type[BaseHTTPRequestHandler], dict[str, Any]]:
    result: dict[str, Any] = {
        "code": None,
        "state": None,
        "error": None,
        "error_description": None,
    }

    class _SpotifyCallbackHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != expected_path:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found.")
                return

            params = parse_qs(parsed.query)
            result["code"] = params.get("code", [None])[0]
            result["state"] = params.get("state", [None])[0]
            result["error"] = params.get("error", [None])[0]
            result["error_description"] = params.get("error_description", [None])[0]

            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if result["error"]:
                body = "<html><body><h1>Spotify authorization failed.</h1>You can close this tab.</body></html>"
            else:
                body = "<html><body><h1>Spotify authorization received.</h1>You can close this tab.</body></html>"
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

    return _SpotifyCallbackHandler, result


def _spotify_wait_for_callback(
    redirect_uri: str,
    *,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    host, port, path = _spotify_validate_redirect_uri(redirect_uri)
    handler_cls, result = _make_spotify_callback_handler(path)

    class _ReuseHTTPServer(HTTPServer):
        allow_reuse_address = True

    try:
        server = _ReuseHTTPServer((host, port), handler_cls)
    except OSError as exc:
        raise AuthError(
            f"Could not bind Spotify callback server on {host}:{port}: {exc}",
            provider="spotify",
            code="spotify_callback_bind_failed",
        ) from exc

    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    deadline = time.monotonic() + max(5.0, timeout_seconds)
    try:
        while time.monotonic() < deadline:
            if result["code"] or result["error"]:
                return result
            time.sleep(0.1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)
    raise AuthError(
        "Spotify authorization timed out waiting for the local callback.",
        provider="spotify",
        code="spotify_callback_timeout",
    )


def _xai_validate_loopback_redirect_uri(redirect_uri: str) -> tuple[str, int, str]:
    parsed = urlparse(redirect_uri)
    if parsed.scheme != "http":
        raise AuthError(
            "xAI OAuth redirect_uri must use http://127.0.0.1.",
            provider="xai-oauth",
            code="xai_redirect_invalid",
        )
    host = parsed.hostname or ""
    if host != XAI_OAUTH_REDIRECT_HOST:
        raise AuthError(
            "xAI OAuth redirect_uri must point to 127.0.0.1.",
            provider="xai-oauth",
            code="xai_redirect_invalid",
        )
    if not parsed.port:
        raise AuthError(
            "xAI OAuth redirect_uri must include an explicit localhost port.",
            provider="xai-oauth",
            code="xai_redirect_invalid",
        )
    return host, parsed.port, parsed.path or "/"


def _xai_callback_cors_origin(origin: Optional[str]) -> str:
    # CORS allowlist for the loopback callback.  Only xAI's own auth origins
    # are accepted; the redirect_uri itself is bound to 127.0.0.1 and gated by
    # PKCE+state, so additional dev/3p origins are not needed here.
    allowed = {
        "https://accounts.x.ai",
        "https://auth.x.ai",
    }
    return origin if origin in allowed else ""


def _make_xai_callback_handler(expected_path: str) -> tuple[type[BaseHTTPRequestHandler], dict[str, Any]]:
    result: dict[str, Any] = {
        "code": None,
        "state": None,
        "error": None,
        "error_description": None,
    }
    result_lock = threading.Lock()

    class _XAICallbackHandler(BaseHTTPRequestHandler):
        def _maybe_write_cors_headers(self) -> None:
            origin = self.headers.get("Origin")
            allow_origin = _xai_callback_cors_origin(origin)
            if allow_origin:
                self.send_header("Access-Control-Allow-Origin", allow_origin)
                self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")
                self.send_header("Access-Control-Allow-Private-Network", "true")
                self.send_header("Vary", "Origin")

        def do_OPTIONS(self) -> None:  # noqa: N802
            self.send_response(204)
            self._maybe_write_cors_headers()
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            parsed = urlparse(self.path)
            if parsed.path != expected_path:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"Not found.")
                return

            params = parse_qs(parsed.query)
            incoming = {
                "code": params.get("code", [None])[0],
                "state": params.get("state", [None])[0],
                "error": params.get("error", [None])[0],
                "error_description": params.get("error_description", [None])[0],
            }

            # Diagnostic logging — emits at INFO so reporters of loopback bugs
            # (#27385 — "callback received but Janus times out") can produce
            # actionable evidence without a code change.  Logged values are
            # fingerprints / booleans only; no actual code/state strings leak
            # into the log file.  Run with ``JANUS_LOG_LEVEL=INFO`` (or check
            # ``~/.janus/logs/agent.log`` which captures INFO+ unconditionally).
            try:
                logger.info(
                    "xAI loopback callback received: path=%s has_code=%s has_state=%s has_error=%s "
                    "ua=%s",
                    parsed.path,
                    incoming["code"] is not None,
                    incoming["state"] is not None,
                    incoming["error"] is not None,
                    (self.headers.get("User-Agent") or "")[:80],
                )
                if incoming["error"]:
                    logger.info(
                        "xAI loopback callback carries error=%s error_description=%s",
                        incoming["error"],
                        (incoming["error_description"] or "")[:200],
                    )
            except Exception:
                # Logging must never break the OAuth flow.
                pass

            # Treat a hit on the callback path with neither `code` nor `error`
            # as a missing OAuth callback (e.g. xAI's auth backend failed to
            # redirect and the user navigated to the bare loopback URL by hand).
            # Show an explicit "not received" page rather than the success page —
            # otherwise the browser claims authorization succeeded while the CLI
            # is still waiting for a real callback and eventually times out.
            if incoming["code"] is None and incoming["error"] is None:
                self.send_response(400)
                self._maybe_write_cors_headers()
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                body = (
                    "<html><body>"
                    "<h1>xAI authorization not received.</h1>"
                    "<p>No authorization code was present in this callback URL. "
                    "Return to the terminal and re-run "
                    "<code>janus auth add xai-oauth</code> to retry.</p>"
                    "</body></html>"
                )
                self.wfile.write(body.encode("utf-8"))
                return

            # ThreadingHTTPServer allows a fallback/manual callback to complete
            # while a browser connection is stuck.  Once we have a terminal
            # OAuth result (code or error), keep the first one so a later
            # concurrent/invalid callback cannot overwrite state before
            # validation in _xai_oauth_loopback_login().
            with result_lock:
                if not (result["code"] or result["error"]):
                    result.update(incoming)

            self.send_response(200)
            self._maybe_write_cors_headers()
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            if incoming["error"]:
                body = "<html><body><h1>xAI authorization failed.</h1>You can close this tab.</body></html>"
            else:
                body = "<html><body><h1>xAI authorization received.</h1>You can close this tab.</body></html>"
            self.wfile.write(body.encode("utf-8"))

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

    return _XAICallbackHandler, result


def _xai_start_callback_server(
    preferred_port: int = XAI_OAUTH_REDIRECT_PORT,
) -> tuple[HTTPServer, threading.Thread, dict[str, Any], str]:
    host = XAI_OAUTH_REDIRECT_HOST
    expected_path = XAI_OAUTH_REDIRECT_PATH
    handler_cls, result = _make_xai_callback_handler(expected_path)

    class _ReuseHTTPServer(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    ports_to_try = [preferred_port]
    if preferred_port != 0:
        ports_to_try.append(0)
    server = None
    last_error: Optional[OSError] = None
    for port in ports_to_try:
        try:
            server = _ReuseHTTPServer((host, port), handler_cls)
            break
        except OSError as exc:
            last_error = exc
    if server is None:
        raise AuthError(
            f"Could not bind xAI callback server on {host}:{preferred_port}: {last_error}",
            provider="xai-oauth",
            code="xai_callback_bind_failed",
        ) from last_error

    actual_port = int(server.server_address[1])
    redirect_uri = f"http://{host}:{actual_port}{expected_path}"
    thread = threading.Thread(
        target=server.serve_forever,
        kwargs={"poll_interval": 0.1},
        daemon=True,
    )
    thread.start()
    return server, thread, result, redirect_uri


def _xai_wait_for_callback(
    server: HTTPServer,
    thread: threading.Thread,
    result: dict[str, Any],
    *,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(5.0, timeout_seconds)
    try:
        while time.monotonic() < deadline:
            if result["code"] or result["error"]:
                return result
            time.sleep(0.1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)
    # Diagnostic: distinguish "no callback ever arrived" from "callback
    # arrived but result wasn't populated" (#27385).  The per-hit handler
    # also logs at INFO; if neither line appears, xAI's IDP never reached
    # the loopback at all (firewall, port-binding, IPv6/IPv4 mismatch).
    logger.info(
        "xAI loopback wait timed out after %.0fs with no usable callback "
        "(result.code=%s result.error=%s)",
        max(5.0, timeout_seconds),
        result["code"] is not None,
        result["error"] is not None,
    )
    raise AuthError(
        "xAI authorization timed out waiting for the local callback.",
        provider="xai-oauth",
        code="xai_callback_timeout",
    )


def _spotify_token_payload_to_state(
    token_payload: Dict[str, Any],
    *,
    client_id: str,
    redirect_uri: str,
    requested_scope: str,
    accounts_base_url: str,
    api_base_url: str,
    previous_state: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    now = datetime.now(timezone.utc)
    expires_in = _coerce_ttl_seconds(token_payload.get("expires_in", 0))
    expires_at = datetime.fromtimestamp(now.timestamp() + expires_in, tz=timezone.utc)
    state = dict(previous_state or {})
    state.update({
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "accounts_base_url": accounts_base_url,
        "api_base_url": api_base_url,
        "scope": requested_scope,
        "granted_scope": str(token_payload.get("scope") or requested_scope).strip(),
        "token_type": str(token_payload.get("token_type", "Bearer") or "Bearer").strip() or "Bearer",
        "access_token": str(token_payload.get("access_token", "") or "").strip(),
        "refresh_token": str(
            token_payload.get("refresh_token")
            or state.get("refresh_token")
            or ""
        ).strip(),
        "obtained_at": now.isoformat(),
        "expires_at": expires_at.isoformat(),
        "expires_in": expires_in,
        "auth_type": "oauth_pkce",
    })
    return state


def _spotify_exchange_code_for_tokens(
    *,
    client_id: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    accounts_base_url: str,
    timeout_seconds: float = 20.0,
) -> Dict[str, Any]:
    try:
        response = httpx.post(
            f"{accounts_base_url}/api/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_id": client_id,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise AuthError(
            f"Spotify token exchange failed: {exc}",
            provider="spotify",
            code="spotify_token_exchange_failed",
        ) from exc

    if response.status_code >= 400:
        detail = response.text.strip()
        raise AuthError(
            "Spotify token exchange failed."
            + (f" Response: {detail}" if detail else ""),
            provider="spotify",
            code="spotify_token_exchange_failed",
        )
    payload = response.json()
    if not isinstance(payload, dict) or not str(payload.get("access_token", "") or "").strip():
        raise AuthError(
            "Spotify token response did not include an access_token.",
            provider="spotify",
            code="spotify_token_exchange_invalid",
        )
    return payload


def _refresh_spotify_oauth_state(
    state: Dict[str, Any],
    *,
    timeout_seconds: float = 20.0,
) -> Dict[str, Any]:
    refresh_token = str(state.get("refresh_token", "") or "").strip()
    if not refresh_token:
        raise AuthError(
            "Spotify refresh token missing. Run `janus auth spotify` again.",
            provider="spotify",
            code="spotify_refresh_token_missing",
            relogin_required=True,
        )

    client_id = _spotify_client_id(state=state)
    accounts_base_url = _spotify_accounts_base_url(state)
    try:
        response = httpx.post(
            f"{accounts_base_url}/api/token",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": client_id,
            },
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise AuthError(
            f"Spotify token refresh failed: {exc}",
            provider="spotify",
            code="spotify_refresh_failed",
        ) from exc

    if response.status_code >= 400:
        detail = response.text.strip()
        raise AuthError(
            "Spotify token refresh failed. Run `janus auth spotify` again."
            + (f" Response: {detail}" if detail else ""),
            provider="spotify",
            code="spotify_refresh_failed",
            relogin_required=True,
        )

    payload = response.json()
    if not isinstance(payload, dict) or not str(payload.get("access_token", "") or "").strip():
        raise AuthError(
            "Spotify refresh response did not include an access_token.",
            provider="spotify",
            code="spotify_refresh_invalid",
            relogin_required=True,
        )

    return _spotify_token_payload_to_state(
        payload,
        client_id=client_id,
        redirect_uri=_spotify_redirect_uri(state=state),
        requested_scope=str(state.get("scope") or DEFAULT_SPOTIFY_SCOPE),
        accounts_base_url=accounts_base_url,
        api_base_url=_spotify_api_base_url(state),
        previous_state=state,
    )


def resolve_spotify_runtime_credentials(
    *,
    force_refresh: bool = False,
    refresh_if_expiring: bool = True,
    refresh_skew_seconds: int = SPOTIFY_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
) -> Dict[str, Any]:
    with _auth_store_lock():
        auth_store = _load_auth_store()
        state = _load_provider_state(auth_store, "spotify")
        if not state:
            raise AuthError(
                "Spotify is not authenticated. Run `janus auth spotify` first.",
                provider="spotify",
                code="spotify_auth_missing",
                relogin_required=True,
            )

        should_refresh = bool(force_refresh)
        if not should_refresh and refresh_if_expiring:
            should_refresh = _is_expiring(state.get("expires_at"), refresh_skew_seconds)
        if should_refresh:
            state = _refresh_spotify_oauth_state(state)
            _store_provider_state(auth_store, "spotify", state, set_active=False)
            _save_auth_store(auth_store)

    access_token = str(state.get("access_token", "") or "").strip()
    if not access_token:
        raise AuthError(
            "Spotify access token missing. Run `janus auth spotify` again.",
            provider="spotify",
            code="spotify_access_token_missing",
            relogin_required=True,
        )

    return {
        "provider": "spotify",
        "access_token": access_token,
        "api_key": access_token,
        "token_type": str(state.get("token_type", "Bearer") or "Bearer"),
        "base_url": _spotify_api_base_url(state),
        "scope": str(state.get("granted_scope") or state.get("scope") or "").strip(),
        "client_id": _spotify_client_id(state=state),
        "redirect_uri": _spotify_redirect_uri(state=state),
        "expires_at": state.get("expires_at"),
        "refresh_token": str(state.get("refresh_token", "") or "").strip(),
    }


def get_spotify_auth_status() -> Dict[str, Any]:
    state = get_provider_auth_state("spotify")
    if not state:
        return {"logged_in": False}

    expires_at = state.get("expires_at")
    refresh_token = str(state.get("refresh_token", "") or "").strip()
    return {
        "logged_in": bool(refresh_token or not _is_expiring(expires_at, 0)),
        "auth_type": state.get("auth_type", "oauth_pkce"),
        "client_id": state.get("client_id"),
        "redirect_uri": state.get("redirect_uri"),
        "scope": state.get("granted_scope") or state.get("scope"),
        "expires_at": expires_at,
        "api_base_url": state.get("api_base_url"),
        "has_refresh_token": bool(refresh_token),
    }


def _spotify_interactive_setup(redirect_uri_hint: str) -> str:
    """Walk the user through creating a Spotify developer app, persist the
    resulting client_id to ~/.janus/.env, and return it.

    Raises SystemExit if the user aborts or submits an empty value.
    """
    from janus_cli.config import save_env_value

    print()
    print("=" * 70)
    print("Spotify first-time setup")
    print("=" * 70)
    print()
    print("Spotify requires every user to register their own lightweight")
    print("developer app. This takes about two minutes and only has to be")
    print("done once per machine.")
    print()
    print(f"Full guide: {SPOTIFY_DOCS_URL}")
    print()
    print("Steps:")
    print(f"  1. Opening {SPOTIFY_DASHBOARD_URL} in your browser...")
    print("  2. Click 'Create app' and fill in:")
    print("       App name:     anything (e.g. janus-agent)")
    print("       Description:  anything")
    print(f"       Redirect URI: {redirect_uri_hint}")
    print("       API/SDK:      Web API")
    print("  3. Agree to the terms, click Save.")
    print("  4. Open the app's Settings page and copy the Client ID.")
    print("  5. Paste it below.")
    print()

    if not _is_remote_session():
        try:
            webbrowser.open(SPOTIFY_DASHBOARD_URL)
        except Exception:
            pass

    try:
        raw = input("Spotify Client ID: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        raise SystemExit("Spotify setup cancelled.")

    if not raw:
        print()
        print(f"No Client ID entered. See {SPOTIFY_DOCS_URL} for the full guide.")
        raise SystemExit("Spotify setup cancelled: empty Client ID.")

    # Persist so subsequent `janus auth spotify` runs skip the wizard.
    save_env_value("JANUS_SPOTIFY_CLIENT_ID", raw)
    # Only persist the redirect URI if it's non-default, to avoid pinning
    # users to a value the default might later change to.
    if redirect_uri_hint and redirect_uri_hint != DEFAULT_SPOTIFY_REDIRECT_URI:
        save_env_value("JANUS_SPOTIFY_REDIRECT_URI", redirect_uri_hint)

    print()
    print("Saved JANUS_SPOTIFY_CLIENT_ID to ~/.janus/.env")
    print()
    return raw


def login_spotify_command(args) -> None:
    existing_state = get_provider_auth_state("spotify") or {}

    # Interactive wizard: if no client_id is configured anywhere, walk the
    # user through creating the Spotify developer app instead of crashing
    # with "JANUS_SPOTIFY_CLIENT_ID is required".
    explicit_client_id = getattr(args, "client_id", None)
    try:
        client_id = _spotify_client_id(explicit_client_id, existing_state)
    except AuthError as exc:
        if getattr(exc, "code", "") != "spotify_client_id_missing":
            raise
        client_id = _spotify_interactive_setup(
            redirect_uri_hint=getattr(args, "redirect_uri", None) or DEFAULT_SPOTIFY_REDIRECT_URI,
        )

    redirect_uri = _spotify_redirect_uri(getattr(args, "redirect_uri", None), existing_state)
    scope = _spotify_scope_string(getattr(args, "scope", None) or existing_state.get("scope"))
    accounts_base_url = _spotify_accounts_base_url(existing_state)
    api_base_url = _spotify_api_base_url(existing_state)
    open_browser = not getattr(args, "no_browser", False)

    code_verifier = _spotify_code_verifier()
    code_challenge = _spotify_code_challenge(code_verifier)
    state_nonce = uuid.uuid4().hex
    authorize_url = _spotify_build_authorize_url(
        client_id=client_id,
        redirect_uri=redirect_uri,
        scope=scope,
        state=state_nonce,
        code_challenge=code_challenge,
        accounts_base_url=accounts_base_url,
    )

    print("Starting Spotify PKCE login...")
    print(f"Client ID: {client_id}")
    print(f"Redirect URI: {redirect_uri}")
    print("Make sure this redirect URI is allow-listed in your Spotify app settings.")
    print()
    print("Open this URL to authorize Janus:")
    print(authorize_url)
    print()
    print(f"Full setup guide: {SPOTIFY_DOCS_URL}")
    print()

    _print_loopback_ssh_hint(redirect_uri, docs_url=SPOTIFY_DOCS_URL)

    if open_browser and not _is_remote_session() and _can_open_graphical_browser():
        try:
            opened = webbrowser.open(authorize_url)
        except Exception:
            opened = False
        if opened:
            print("Browser opened for Spotify authorization.")
        else:
            print("Could not open the browser automatically; use the URL above.")

    callback = _spotify_wait_for_callback(
        redirect_uri,
        timeout_seconds=float(getattr(args, "timeout", None) or 180.0),
    )
    if callback.get("error"):
        detail = callback.get("error_description") or callback["error"]
        raise SystemExit(f"Spotify authorization failed: {detail}")
    if callback.get("state") != state_nonce:
        raise SystemExit("Spotify authorization failed: state mismatch.")

    token_payload = _spotify_exchange_code_for_tokens(
        client_id=client_id,
        code=str(callback.get("code") or ""),
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
        accounts_base_url=accounts_base_url,
        timeout_seconds=float(getattr(args, "timeout", None) or 20.0),
    )
    spotify_state = _spotify_token_payload_to_state(
        token_payload,
        client_id=client_id,
        redirect_uri=redirect_uri,
        requested_scope=scope,
        accounts_base_url=accounts_base_url,
        api_base_url=api_base_url,
    )

    with _auth_store_lock():
        auth_store = _load_auth_store()
        _store_provider_state(auth_store, "spotify", spotify_state, set_active=False)
        saved_to = _save_auth_store(auth_store)

    print("Spotify login successful!")
    print(f"  Auth state: {saved_to}")
    print("  Provider state saved under providers.spotify")
    print(f"  Docs: {SPOTIFY_DOCS_URL}")

# =============================================================================
# SSH / remote session detection
# =============================================================================

def _is_remote_session() -> bool:
    """Detect environments where loopback OAuth can't reach the local browser.

    Historically only SSH was checked, but #26923 surfaced that
    **browser-only remote consoles** (GCP Cloud Shell, GitHub
    Codespaces, AWS EC2 Instance Connect, Gitpod, Replit, etc.) hit
    the exact same problem — the user has a browser on their laptop
    but the loopback listener is bound on the remote VM that the
    laptop's browser can't reach.  These environments typically don't
    set ``SSH_CLIENT`` / ``SSH_TTY``, so the SSH-only check left
    them with no guidance and no fallback.
    """
    if os.getenv("SSH_CLIENT") or os.getenv("SSH_TTY"):
        return True
    # Browser-only remote IDEs / cloud shells.  Keep this list narrow
    # (well-known, documented env vars set by the host platform) so
    # we don't falsely trip on a developer's local shell.
    for var in (
        "CLOUD_SHELL",         # GCP Cloud Shell
        "CODESPACES",          # GitHub Codespaces
        "CODESPACE_NAME",      # GitHub Codespaces (alt)
        "GITPOD_WORKSPACE_ID", # Gitpod
        "REPL_ID",             # Replit
        "STACKBLITZ",          # StackBlitz
    ):
        if os.getenv(var):
            return True
    return False


# Console/text-mode browsers that ``webbrowser`` will happily launch INSIDE
# the terminal.  Opening one of these is worse than not opening anything —
# it hijacks the user's TTY with an unusable text browser (the xAI OAuth
# "Account Management" page rendered in w3m, reported May 2026) instead of
# letting them copy the URL to a real browser.  When the resolved browser is
# one of these we refuse to auto-open and fall back to the print-the-URL /
# manual-paste path, same as a remote session.
_CONSOLE_BROWSER_NAMES: FrozenSet[str] = frozenset(
    {
        "w3m",
        "lynx",
        "links",
        "links2",
        "elinks",
        "www-browser",
        "browsh",  # TUI browser — still hijacks the terminal
    }
)


def _can_open_graphical_browser() -> bool:
    """Return True only when a *graphical* browser is likely to open.

    ``webbrowser.open()`` resolves to whatever the platform offers, and on a
    headless / CLI-only Linux box with no GUI browser installed that is often
    a text-mode browser (w3m/lynx/links) which launches inside the terminal
    and takes over the user's session.  This guard distinguishes "a real
    windowed browser will pop up" from "a console browser will hijack the
    TTY", so callers can fall back to printing the URL instead.

    Heuristics:
      * Respect ``$BROWSER`` — if it names a known console browser, refuse.
      * On Linux, require a display server (``$DISPLAY`` / ``$WAYLAND_DISPLAY``)
        unless ``$BROWSER`` points at something graphical; no display server
        almost always means no GUI browser.
      * Ask ``webbrowser.get()`` what it resolved to and refuse when the
        underlying command is a known console browser.
      * macOS and Windows always have a usable default GUI browser.
    """
    import webbrowser as _webbrowser

    def _names_console_browser(value: str) -> bool:
        token = value.strip().split()[0] if value.strip() else ""
        base = os.path.basename(token).lower()
        return base in _CONSOLE_BROWSER_NAMES

    browser_env = os.environ.get("BROWSER", "")
    if browser_env and _names_console_browser(browser_env):
        return False

    if sys.platform.startswith("linux"):
        has_display = bool(
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        )
        # An explicit graphical $BROWSER can work without $DISPLAY in odd
        # setups, but a console $BROWSER already returned False above, so the
        # only way to reach here with a $BROWSER set is a graphical one.
        if not has_display and not browser_env:
            return False

    try:
        controller = _webbrowser.get()
    except Exception:
        # No browser resolvable at all → definitely don't auto-open.
        return False

    candidate = (
        getattr(controller, "name", "")
        or getattr(controller, "basename", "")
        or ""
    )
    if candidate and _names_console_browser(candidate):
        return False

    return True


def _parse_pasted_callback(raw: str) -> dict:
    """Parse a pasted callback URL / query string into the loopback shape.

    Accepts any of:

    * full URL:  ``http://127.0.0.1:56121/callback?code=abc&state=xyz``
    * bare query string:  ``?code=abc&state=xyz``  or  ``code=abc&state=xyz``
    * bare code (no state, only used when the upstream omits state):
      ``abc-the-code-value``

    Returns ``{"code", "state", "error", "error_description"}`` with
    missing keys set to ``None`` so the loopback callsites can keep
    using the same validation path (state check, error check, etc.)
    they already use for the HTTP server output.  Regression for
    #26923 — formalises the curl-the-callback-URL workaround the
    reporter used while waiting for upstream support.
    """
    stripped = raw.strip()
    result: dict = {
        "code": None,
        "state": None,
        "error": None,
        "error_description": None,
    }
    if not stripped:
        return result
    query = ""
    if stripped.startswith(("http://", "https://")):
        try:
            parsed = urlparse(stripped)
        except Exception:
            return result
        query = parsed.query or ""
    elif stripped.startswith("?"):
        query = stripped[1:]
    elif "=" in stripped:
        # Looks like a bare query fragment (``code=...&state=...``).
        query = stripped
    else:
        # Treat as a bare opaque code value with no state.
        result["code"] = stripped
        return result
    params = parse_qs(query, keep_blank_values=False)
    for key in ("code", "state", "error", "error_description"):
        values = params.get(key)
        if values:
            result[key] = values[0]
    return result


def _prompt_manual_callback_paste(redirect_uri: str) -> dict:
    """Read a callback URL from stdin as a fallback for browser-only remotes.

    Used when ``--manual-paste`` is set or when the loopback listener
    cannot bind.  Returns the parsed callback dict (same shape as the
    HTTP handler output) so the existing state / error validation in
    the caller works unchanged.  See #26923.
    """
    print()
    print("─── Manual callback paste ─────────────────────────────────────")
    print("After approving in your browser, your browser will try to load")
    print(f"  {redirect_uri}")
    print("which fails (the loopback listener is on this remote machine,")
    print("not on your laptop) — that is expected.  Copy the FULL URL")
    print("from your browser's address bar of that failed page and paste")
    print("it below.  A bare '?code=...&state=...' fragment also works.")
    print("If the consent page shows the authorization code in-page")
    print("(xAI's current behavior) rather than redirecting, paste the")
    print("bare code value on its own.")
    print("───────────────────────────────────────────────────────────────")
    try:
        raw = input("Callback URL: ")
    except (EOFError, KeyboardInterrupt):
        raw = ""
    return _parse_pasted_callback(raw)


def _ssh_user_at_host() -> str:
    """Return best-effort 'user@hostname' for the SSH tunnel hint command.

    Falls back to placeholder tokens when the values cannot be determined so
    the hint is always syntactically valid even if not copy-pasteable.
    """
    try:
        import socket as _socket
        hostname = _socket.gethostname() or "<this-host>"
    except OSError:
        hostname = "<this-host>"
    user = os.getenv("USER") or os.getenv("LOGNAME") or "<user>"
    return f"{user}@{hostname}"


def _print_loopback_ssh_hint(redirect_uri: str, *, docs_url: str | None = None) -> None:
    """Print an SSH tunnel hint when running a loopback-redirect OAuth flow on a
    remote host. The auth server (xAI, Spotify, ...) will redirect the user's
    browser to ``127.0.0.1:<port>/callback``. If the browser is on a different
    machine than the loopback listener (the usual SSH case), the redirect can't
    reach the listener without a local port forward.

    The hint is best-effort: silent if we don't think we're remote, or if we
    can't parse a host/port out of the redirect URI.

    Pass ``docs_url`` for a provider-specific guide (e.g. the xAI Grok OAuth
    page); the generic OAuth-over-SSH guide is always shown after it.
    """
    if not _is_remote_session():
        return
    try:
        parsed = urlparse(redirect_uri)
    except Exception:
        return
    host = parsed.hostname or ""
    port = parsed.port
    if host not in {"127.0.0.1", "::1", "localhost"} or not port:
        return
    divider = "-" * 60
    print()
    print(divider)
    print("Remote session detected — SSH tunnel required")
    print(divider)
    print(f"Janus is waiting for the OAuth callback on {redirect_uri}")
    print("but your browser is on a different machine. Run this command")
    print("in a NEW terminal on your local machine BEFORE opening the URL:")
    print()
    print(f"  ssh -N -L {port}:127.0.0.1:{port} {_ssh_user_at_host()}")
    print()
    print("Then open the authorize URL above in your local browser.")
    print()
    print("No SSH client (Cloud Shell / Codespaces / web IDE)?  Re-run with")
    print("`--manual-paste` to skip the loopback listener and paste the failed")
    print("callback URL directly.")
    if docs_url:
        print(f"Provider docs:      {docs_url}")
    print(f"SSH/jump-box guide: {OAUTH_OVER_SSH_DOCS_URL}")
    print(divider)
    print()


# =============================================================================
# OpenAI Codex auth — tokens stored in ~/.janus/auth.json (not ~/.codex/)
#
# Janus maintains its own Codex OAuth session separate from the Codex CLI
# and VS Code extension. This prevents refresh token rotation conflicts
# where one app's refresh invalidates the other's session.
# =============================================================================

def _read_codex_tokens(*, _lock: bool = True) -> Dict[str, Any]:
    """Read Codex OAuth tokens from Janus auth store (~/.janus/auth.json).
    
    Returns dict with 'tokens' (access_token, refresh_token) and 'last_refresh'.
    Raises AuthError if no Codex tokens are stored.
    """
    if _lock:
        with _auth_store_lock():
            auth_store = _load_auth_store()
    else:
        auth_store = _load_auth_store()
    state = _load_provider_state(auth_store, "openai-codex")
    if not state:
        raise AuthError(
            "No Codex credentials stored. Run `janus auth` to authenticate.",
            provider="openai-codex",
            code="codex_auth_missing",
            relogin_required=True,
        )
    tokens = state.get("tokens")
    if not isinstance(tokens, dict):
        raise AuthError(
            "Codex auth state is missing tokens. Run `janus auth` to re-authenticate.",
            provider="openai-codex",
            code="codex_auth_invalid_shape",
            relogin_required=True,
        )
    access_token = tokens.get("access_token")
    refresh_token = tokens.get("refresh_token")
    if not isinstance(access_token, str) or not access_token.strip():
        raise AuthError(
            "Codex auth is missing access_token. Run `janus auth` to re-authenticate.",
            provider="openai-codex",
            code="codex_auth_missing_access_token",
            relogin_required=True,
        )
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise AuthError(
            "Codex auth is missing refresh_token. Run `janus auth` to re-authenticate.",
            provider="openai-codex",
            code="codex_auth_missing_refresh_token",
            relogin_required=True,
        )
    return {
        "tokens": tokens,
        "last_refresh": state.get("last_refresh"),
    }


def _sync_codex_pool_entries(
    auth_store: Dict[str, Any],
    tokens: Dict[str, str],
    last_refresh: Optional[str],
) -> None:
    """Mirror a fresh Codex re-auth into the credential_pool OAuth entries.

    The runtime selects credentials from ``credential_pool.openai-codex``, not
    from ``providers.openai-codex.tokens``.  A re-auth invalidates the prior
    OAuth pair server-side, but pool entries keep holding the now-consumed
    refresh token plus any stale error markers — so the next request spends a
    dead token and gets a 401 ``token_invalidated``.

    What gets refreshed:

    * ``device_code`` — the singleton-seeded entry written by the device-code
      OAuth flow when the user logged in via ``janus setup`` / the model
      picker.  Always synced with the fresh tokens.
    * ``manual:device_code`` — entries created by ``janus auth add openai-codex``
      that use the same device-code OAuth mechanism.  An interactive re-auth
      proves the user owns the ChatGPT account, so it is safe (and expected)
      to refresh these entries too.  Without this, a user who once ran the
      ``janus auth add`` workaround for #33000 would silently leave that
      manual entry stale on every subsequent re-auth, recreating the issue
      reported in #33538.

    What does NOT get refreshed:

    * ``manual:api_key`` and any other non-device-code manual sources — those
      are independent credentials (an explicit API key, a different ChatGPT
      account, etc.) and must not be overwritten by a single re-auth.

    Error markers (``last_status``, ``last_error_*``) are also cleared on
    every device-code-backed entry — even those whose tokens we did not
    rewrite — so that an interactive re-auth gives every relevant pool entry
    a fresh selection chance instead of leaving them marked unhealthy from a
    pre-re-auth 401.
    """
    access_token = tokens.get("access_token")
    if not access_token:
        return
    refresh_token = tokens.get("refresh_token")
    pool = auth_store.get("credential_pool")
    if not isinstance(pool, dict):
        return
    entries = pool.get("openai-codex")
    if not isinstance(entries, list):
        return
    # Sources whose tokens should be rewritten by a fresh Codex device-code
    # OAuth re-auth.  ``manual:api_key`` and unknown sources are intentionally
    # excluded — they represent independent credentials.
    REFRESHABLE_SOURCES = {"device_code", "manual:device_code"}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source = entry.get("source")
        if source not in REFRESHABLE_SOURCES:
            continue
        entry["access_token"] = access_token
        if refresh_token:
            entry["refresh_token"] = refresh_token
        if last_refresh:
            entry["last_refresh"] = last_refresh
        entry["last_status"] = None
        entry["last_status_at"] = None
        entry["last_error_code"] = None
        entry["last_error_reason"] = None
        entry["last_error_message"] = None
        entry["last_error_reset_at"] = None


def _save_codex_tokens(tokens: Dict[str, str], last_refresh: str = None, label: str = None) -> None:
    """Save Codex OAuth tokens to Janus auth store (~/.janus/auth.json)."""
    if last_refresh is None:
        last_refresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with _auth_store_lock():
        auth_store = _load_auth_store()
        state = _load_provider_state(auth_store, "openai-codex") or {}
        state["tokens"] = tokens
        state["last_refresh"] = last_refresh
        state["auth_mode"] = "chatgpt"
        if label and str(label).strip():
            state["label"] = str(label).strip()
        _save_provider_state(auth_store, "openai-codex", state)
        _sync_codex_pool_entries(auth_store, tokens, last_refresh)
        _save_auth_store(auth_store)


def refresh_codex_oauth_pure(
    access_token: str,
    refresh_token: str,
    *,
    timeout_seconds: float = 20.0,
) -> Dict[str, Any]:
    """Refresh Codex OAuth tokens without mutating Janus auth state."""
    del access_token  # Access token is only used by callers to decide whether to refresh.
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise AuthError(
            "Codex auth is missing refresh_token. Run `janus auth` to re-authenticate.",
            provider="openai-codex",
            code="codex_auth_missing_refresh_token",
            relogin_required=True,
        )

    timeout = httpx.Timeout(max(5.0, float(timeout_seconds)))
    with httpx.Client(timeout=timeout, headers={"Accept": "application/json"}) as client:
        response = client.post(
            CODEX_OAUTH_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CODEX_OAUTH_CLIENT_ID,
            },
        )

    if response.status_code == 429:
        # Upstream rate-limit / usage-quota exhaustion on the token endpoint.
        # The stored refresh token is still valid here — re-authenticating
        # cannot lift a quota cap. Classify distinctly from auth failures so
        # callers surface a "retry later" notice instead of a misleading
        # "run janus auth" prompt (see issue #32790).
        retry_after = _parse_retry_after_seconds(getattr(response, "headers", None))
        if retry_after is not None:
            message = (
                f"Codex provider quota exhausted (429); retry after {retry_after}s. "
                "Credentials are still valid."
            )
        else:
            message = (
                "Codex provider quota exhausted (429). Credentials are still valid; "
                "retry after the usage limit resets."
            )
        raise AuthError(
            message,
            provider="openai-codex",
            code=CODEX_RATE_LIMITED_CODE,
            relogin_required=False,
        )

    if response.status_code != 200:
        code = "codex_refresh_failed"
        message = f"Codex token refresh failed with status {response.status_code}."
        relogin_required = False
        try:
            err = response.json()
            if isinstance(err, dict):
                err_obj = err.get("error")
                # OpenAI shape: {"error": {"code": "...", "message": "...", "type": "..."}}
                if isinstance(err_obj, dict):
                    nested_code = err_obj.get("code") or err_obj.get("type")
                    if isinstance(nested_code, str) and nested_code.strip():
                        code = nested_code.strip()
                    nested_msg = err_obj.get("message")
                    if isinstance(nested_msg, str) and nested_msg.strip():
                        message = f"Codex token refresh failed: {nested_msg.strip()}"
                # OAuth spec shape: {"error": "code_str", "error_description": "..."}
                elif isinstance(err_obj, str) and err_obj.strip():
                    code = err_obj.strip()
                    err_desc = err.get("error_description") or err.get("message")
                    if isinstance(err_desc, str) and err_desc.strip():
                        message = f"Codex token refresh failed: {err_desc.strip()}"
        except Exception:
            pass
        if code in {"invalid_grant", "invalid_token", "invalid_request"}:
            relogin_required = True
        if code == "refresh_token_reused":
            message = (
                "Codex refresh token was already consumed by another client "
                "(e.g. Codex CLI or VS Code extension). "
                "Run `codex` in your terminal to generate fresh tokens, "
                "then run `janus auth` to re-authenticate."
            )
            relogin_required = True
        # A 401/403 from the token endpoint always means the refresh token
        # is invalid/expired — force relogin even if the body error code
        # wasn't one of the known strings above.
        if response.status_code in {401, 403} and not relogin_required:
            relogin_required = True
        raise AuthError(
            message,
            provider="openai-codex",
            code=code,
            relogin_required=relogin_required,
        )

    try:
        refresh_payload = response.json()
    except Exception as exc:
        raise AuthError(
            "Codex token refresh returned invalid JSON.",
            provider="openai-codex",
            code="codex_refresh_invalid_json",
            relogin_required=True,
        ) from exc

    refreshed_access = refresh_payload.get("access_token")
    if not isinstance(refreshed_access, str) or not refreshed_access.strip():
        raise AuthError(
            "Codex token refresh response was missing access_token.",
            provider="openai-codex",
            code="codex_refresh_missing_access_token",
            relogin_required=True,
        )

    updated = {
        "access_token": refreshed_access.strip(),
        "refresh_token": refresh_token.strip(),
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    next_refresh = refresh_payload.get("refresh_token")
    if isinstance(next_refresh, str) and next_refresh.strip():
        updated["refresh_token"] = next_refresh.strip()
    return updated


def _refresh_codex_auth_tokens(
    tokens: Dict[str, str],
    timeout_seconds: float,
) -> Dict[str, str]:
    """Refresh Codex access token using the refresh token.
    
    Saves the new tokens to Janus auth store automatically.
    """
    refreshed = refresh_codex_oauth_pure(
        str(tokens.get("access_token", "") or ""),
        str(tokens.get("refresh_token", "") or ""),
        timeout_seconds=timeout_seconds,
    )
    updated_tokens = dict(tokens)
    updated_tokens["access_token"] = refreshed["access_token"]
    updated_tokens["refresh_token"] = refreshed["refresh_token"]

    _save_codex_tokens(updated_tokens)
    return updated_tokens


def _import_codex_cli_tokens() -> Optional[Dict[str, str]]:
    """Try to read tokens from ~/.codex/auth.json (Codex CLI shared file).
    
    Returns tokens dict if valid and not expired, None otherwise.
    Does NOT write to the shared file.
    """
    codex_home = os.getenv("CODEX_HOME", "").strip()
    if not codex_home:
        codex_home = str(Path.home() / ".codex")
    auth_path = Path(codex_home).expanduser() / "auth.json"
    if not auth_path.is_file():
        return None
    try:
        payload = json.loads(auth_path.read_text())
        tokens = payload.get("tokens")
        if not isinstance(tokens, dict):
            return None
        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")
        if not access_token or not refresh_token:
            return None
        # Reject expired tokens — importing stale tokens from ~/.codex/
        # that can't be refreshed leaves the user stuck with "Login successful!"
        # but no working credentials.
        if _codex_access_token_is_expiring(access_token, 0):
            logger.debug(
                "Codex CLI tokens at %s are expired — skipping import.", auth_path,
            )
            return None
        return dict(tokens)
    except Exception:
        return None


def resolve_codex_runtime_credentials(
    *,
    force_refresh: bool = False,
    refresh_if_expiring: bool = True,
    refresh_skew_seconds: int = CODEX_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
) -> Dict[str, Any]:
    """Resolve runtime credentials from Janus's own Codex token store.

    Falls back to the credential pool when the singleton (``providers.openai-codex.tokens``)
    has no usable access_token but the pool (``credential_pool.openai-codex``) does. This
    closes the divergence between the chat path (singleton-only via this function) and
    the auxiliary path (pool-first via ``_read_codex_access_token``). Without this
    fallback, a user whose tokens live only in the pool — for example after a manual
    pool seed, a partial re-auth, or pool-only restoration from a backup — gets a bare
    HTTP 401 ``Missing Authentication header`` from the wire instead of a usable
    credential. See issue #32992.
    """
    try:
        data = _read_codex_tokens()
    except AuthError:
        pool_token = _pool_codex_access_token()
        if pool_token:
            base_url = (
                os.getenv("JANUS_CODEX_BASE_URL", "").strip().rstrip("/")
                or DEFAULT_CODEX_BASE_URL
            )
            return {
                "provider": "openai-codex",
                "base_url": base_url,
                "api_key": pool_token,
                "source": "credential_pool",
                "last_refresh": None,
                "auth_mode": "chatgpt",
            }
        raise

    tokens = dict(data["tokens"])
    access_token = str(tokens.get("access_token", "") or "").strip()
    refresh_timeout_seconds = float(os.getenv("JANUS_CODEX_REFRESH_TIMEOUT_SECONDS", "20"))

    should_refresh = bool(force_refresh)
    if (not should_refresh) and refresh_if_expiring:
        should_refresh = _codex_access_token_is_expiring(access_token, refresh_skew_seconds)
    if should_refresh:
        # Re-read under lock to avoid racing with other Janus processes
        with _auth_store_lock(timeout_seconds=max(float(AUTH_LOCK_TIMEOUT_SECONDS), refresh_timeout_seconds + 5.0)):
            data = _read_codex_tokens(_lock=False)
            tokens = dict(data["tokens"])
            access_token = str(tokens.get("access_token", "") or "").strip()

            should_refresh = bool(force_refresh)
            if (not should_refresh) and refresh_if_expiring:
                should_refresh = _codex_access_token_is_expiring(access_token, refresh_skew_seconds)

            if should_refresh:
                tokens = _refresh_codex_auth_tokens(tokens, refresh_timeout_seconds)
                access_token = str(tokens.get("access_token", "") or "").strip()

    base_url = (
        os.getenv("JANUS_CODEX_BASE_URL", "").strip().rstrip("/")
        or DEFAULT_CODEX_BASE_URL
    )

    return {
        "provider": "openai-codex",
        "base_url": base_url,
        "api_key": access_token,
        "source": "janus-auth-store",
        "last_refresh": data.get("last_refresh"),
        "auth_mode": "chatgpt",
    }


def _pool_codex_access_token() -> str:
    """Return the most-recent usable access_token from the openai-codex pool.

    Used as a fallback by ``resolve_codex_runtime_credentials`` when the
    singleton has no creds.  Reads ``credential_pool.openai-codex`` entries
    directly from auth.json and picks the first non-empty access_token,
    preferring entries that are not currently in an exhaustion cooldown.
    Returns ``""`` when no usable entry is found (caller handles by raising
    the original AuthError).
    """
    try:
        with _auth_store_lock():
            auth_store = _load_auth_store()
        pool = auth_store.get("credential_pool")
        if not isinstance(pool, dict):
            return ""
        entries = pool.get("openai-codex")
        if not isinstance(entries, list):
            return ""

        def _entry_usable(entry: Dict[str, Any]) -> bool:
            if not isinstance(entry, dict):
                return False
            token = entry.get("access_token")
            if not isinstance(token, str) or not token.strip():
                return False
            # Skip entries currently in an exhaustion cooldown window.
            reset_at = entry.get("last_error_reset_at")
            if isinstance(reset_at, (int, float)) and reset_at > time.time():
                return False
            return True

        for entry in entries:
            if _entry_usable(entry):
                return str(entry.get("access_token", "")).strip()
    except Exception:
        logger.debug("Codex pool fallback lookup failed", exc_info=True)
    return ""


# =============================================================================
# xAI Grok OAuth — tokens stored in ~/.janus/auth.json
# =============================================================================

def _read_xai_oauth_tokens(*, _lock: bool = True) -> Dict[str, Any]:
    if _lock:
        with _auth_store_lock():
            auth_store = _load_auth_store()
    else:
        auth_store = _load_auth_store()
    state = _load_provider_state(auth_store, "xai-oauth")
    if not state:
        raise AuthError(
            "No xAI OAuth credentials stored. Select xAI Grok OAuth (SuperGrok / Premium+) in `janus model`.",
            provider="xai-oauth",
            code="xai_auth_missing",
            relogin_required=True,
        )
    tokens = state.get("tokens")
    if not isinstance(tokens, dict):
        raise AuthError(
            "xAI OAuth state is missing tokens. Re-authenticate with `janus model`.",
            provider="xai-oauth",
            code="xai_auth_invalid_shape",
            relogin_required=True,
        )
    access_token = str(tokens.get("access_token", "") or "").strip()
    refresh_token = str(tokens.get("refresh_token", "") or "").strip()
    if not access_token:
        raise AuthError(
            "xAI OAuth state is missing access_token. Re-authenticate with `janus model`.",
            provider="xai-oauth",
            code="xai_auth_missing_access_token",
            relogin_required=True,
        )
    if not refresh_token:
        raise AuthError(
            "xAI OAuth state is missing refresh_token. Re-authenticate with `janus model`.",
            provider="xai-oauth",
            code="xai_auth_missing_refresh_token",
            relogin_required=True,
        )
    return {
        "tokens": tokens,
        "last_refresh": state.get("last_refresh"),
        "discovery": state.get("discovery") or {},
        "redirect_uri": state.get("redirect_uri"),
    }


def _save_xai_oauth_tokens(
    tokens: Dict[str, Any],
    *,
    discovery: Optional[Dict[str, Any]] = None,
    redirect_uri: str = "",
    last_refresh: Optional[str] = None,
) -> None:
    if last_refresh is None:
        last_refresh = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with _auth_store_lock():
        auth_store = _load_auth_store()
        state = _load_provider_state(auth_store, "xai-oauth") or {}
        state["tokens"] = tokens
        state["last_refresh"] = last_refresh
        state["auth_mode"] = "oauth_pkce"
        if discovery:
            state["discovery"] = discovery
        if redirect_uri:
            state["redirect_uri"] = redirect_uri
        _save_provider_state(auth_store, "xai-oauth", state)
        _save_auth_store(auth_store)


def _xai_access_token_is_expiring(access_token: str, skew_seconds: int = 0) -> bool:
    if not isinstance(access_token, str) or "." not in access_token:
        return False
    try:
        parts = access_token.split(".")
        if len(parts) < 2:
            return False
        payload_b64 = parts[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64.encode("ascii")).decode("utf-8"))
        exp = payload.get("exp")
        if not isinstance(exp, (int, float)):
            return False
        return float(exp) <= (time.time() + max(0, int(skew_seconds)))
    except Exception:
        return False


def _xai_validate_oauth_endpoint(url: str, *, field: str) -> str:
    """Refuse any OIDC discovery endpoint that isn't HTTPS on the xAI origin.

    The OIDC discovery response is a long-lived, low-frequency request whose
    output is cached in ``~/.janus/auth.json``. A single MITM during initial
    login could substitute a malicious ``token_endpoint``; that URL would
    then receive the refresh_token on every subsequent refresh — a permanent
    credential leak from a one-time MITM. Validating scheme + host pins the
    cached endpoint to the xAI auth origin (or a future ``*.x.ai`` subdomain
    if xAI migrates) so the cache poisoning loses its persistence guarantee.

    RFC 8414 §2 requires the issuer to be ``https://`` and SHOULD-keeps the
    token_endpoint on the same origin; we enforce both. ``x.ai`` is the
    bare apex, so we accept either exact host match or any ``.x.ai`` suffix.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise AuthError(
            f"xAI OIDC discovery returned a non-HTTPS {field}: {url!r}.",
            provider="xai-oauth",
            code="xai_discovery_invalid",
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise AuthError(
            f"xAI OIDC discovery {field} is missing a hostname: {url!r}.",
            provider="xai-oauth",
            code="xai_discovery_invalid",
        )
    if host != "x.ai" and not host.endswith(".x.ai"):
        raise AuthError(
            f"xAI OIDC discovery {field} host {host!r} is not on the xAI origin "
            f"(expected x.ai or a *.x.ai subdomain). Refusing to use a cached "
            f"endpoint that may have been substituted by a MITM during initial "
            f"discovery; re-authenticate with `janus model` to re-fetch.",
            provider="xai-oauth",
            code="xai_discovery_invalid",
        )
    return url


def _xai_validate_inference_base_url(value: str, *, fallback: str) -> str:
    """Refuse a non-xAI base_url for the OAuth-authenticated inference path.

    The xAI Grok OAuth bearer is a high-value, long-lived credential tied to
    the user's SuperGrok subscription. ``XAI_BASE_URL`` / ``JANUS_XAI_BASE_URL``
    let users repoint the inference endpoint (handy for staging or a local
    proxy), but the env override is also a credential-leak vector: a tampered
    ``.env`` or hostile shell init that sets
    ``XAI_BASE_URL=https://attacker.example/v1`` would ship the OAuth access
    token to a third party on every request, silently.

    Pin the inference origin to ``api.x.ai`` (or any ``*.x.ai`` subdomain xAI
    may add). On rejection, fall back to the default and log a warning rather
    than raise — a bad env var should not deadlock authentication, but it
    should also never leak the bearer.

    ``value`` is the already-stripped, trailing-slash-trimmed candidate from
    env. Empty input returns ``fallback`` unchanged.
    """
    candidate = (value or "").strip().rstrip("/")
    if not candidate:
        return fallback
    try:
        parsed = urlparse(candidate)
    except Exception:
        logger.warning(
            "Ignoring malformed xAI base_url override %r; using %s instead.",
            candidate, fallback,
        )
        return fallback
    if parsed.scheme != "https":
        logger.warning(
            "Refusing non-HTTPS xAI base_url override %r (xai-oauth bearer would "
            "be sent in cleartext); falling back to %s.",
            candidate, fallback,
        )
        return fallback
    host = (parsed.hostname or "").lower()
    if not host:
        logger.warning(
            "Ignoring xAI base_url override %r with no hostname; using %s instead.",
            candidate, fallback,
        )
        return fallback
    if host != "x.ai" and not host.endswith(".x.ai"):
        logger.warning(
            "Refusing xAI base_url override %r — host %r is not on the xAI origin "
            "(expected x.ai or a *.x.ai subdomain). The xai-oauth bearer is only "
            "valid against xAI's inference API; sending it elsewhere would leak "
            "the credential. Falling back to %s.",
            candidate, host, fallback,
        )
        return fallback
    return candidate


def _xai_oauth_discovery(timeout_seconds: float = 15.0) -> Dict[str, str]:
    try:
        response = httpx.get(
            XAI_OAUTH_DISCOVERY_URL,
            headers={"Accept": "application/json"},
            timeout=timeout_seconds,
        )
    except Exception as exc:
        raise AuthError(
            f"xAI OIDC discovery failed: {exc}",
            provider="xai-oauth",
            code="xai_discovery_failed",
        ) from exc
    if response.status_code != 200:
        raise AuthError(
            f"xAI OIDC discovery returned status {response.status_code}.",
            provider="xai-oauth",
            code="xai_discovery_failed",
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise AuthError(
            f"xAI OIDC discovery returned invalid JSON: {exc}",
            provider="xai-oauth",
            code="xai_discovery_invalid_json",
        ) from exc
    if not isinstance(payload, dict):
        raise AuthError(
            "xAI OIDC discovery response was not a JSON object.",
            provider="xai-oauth",
            code="xai_discovery_incomplete",
        )
    authorization_endpoint = str(payload.get("authorization_endpoint", "") or "").strip()
    token_endpoint = str(payload.get("token_endpoint", "") or "").strip()
    if not authorization_endpoint or not token_endpoint:
        raise AuthError(
            "xAI OIDC discovery response was missing required endpoints.",
            provider="xai-oauth",
            code="xai_discovery_incomplete",
        )
    _xai_validate_oauth_endpoint(authorization_endpoint, field="authorization_endpoint")
    _xai_validate_oauth_endpoint(token_endpoint, field="token_endpoint")
    return {
        "authorization_endpoint": authorization_endpoint,
        "token_endpoint": token_endpoint,
    }


def refresh_xai_oauth_pure(
    access_token: str,
    refresh_token: str,
    *,
    token_endpoint: str = "",
    timeout_seconds: float = 20.0,
) -> Dict[str, Any]:
    del access_token
    if not isinstance(refresh_token, str) or not refresh_token.strip():
        raise AuthError(
            "xAI OAuth is missing refresh_token. Re-authenticate with `janus model`.",
            provider="xai-oauth",
            code="xai_auth_missing_refresh_token",
            relogin_required=True,
        )
    endpoint = token_endpoint.strip() or _xai_oauth_discovery(timeout_seconds)["token_endpoint"]
    # Re-validate cached endpoints on the refresh hot path: an auth.json
    # written by an older Janus (or hand-edited) may carry a non-xAI
    # token_endpoint that would receive every future refresh_token in
    # plaintext if we trusted it blindly. Cheap suffix check; fast-fail
    # with a clear error so the user can re-run `janus model` to refetch.
    _xai_validate_oauth_endpoint(endpoint, field="token_endpoint")
    timeout = httpx.Timeout(max(5.0, float(timeout_seconds)))
    with httpx.Client(timeout=timeout, headers={"Accept": "application/json"}) as client:
        response = client.post(
            endpoint,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "client_id": XAI_OAUTH_CLIENT_ID,
                "refresh_token": refresh_token,
            },
        )
    if response.status_code != 200:
        detail = response.text.strip()
        # ``403`` from xAI's token endpoint is almost always a tier /
        # entitlement gate (the OAuth grant exists but the account isn't
        # on the allowlist for API access).  Re-running ``janus model``
        # won't fix that — surface a separate error code so
        # ``format_auth_error`` doesn't append a misleading
        # re-authenticate hint, and point users at the ``XAI_API_KEY``
        # fallback.  See #26847.
        if response.status_code == 403:
            raise AuthError(
                "xAI token refresh failed with HTTP 403."
                + (f" Response: {detail}" if detail else "")
                + " This OAuth account is not authorized for xAI API"
                  " access — xAI may be restricting API/OAuth use to"
                  " specific SuperGrok tiers despite the in-app"
                  " subscription being active. Re-logging in won't"
                  " change that; set ``XAI_API_KEY`` and switch to"
                  " ``provider: xai`` (API-key path) if available, or"
                  " upgrade your subscription at https://x.ai/grok.",
                provider="xai-oauth",
                code="xai_oauth_tier_denied",
                relogin_required=False,
            )
        raise AuthError(
            "xAI token refresh failed."
            + (f" Response: {detail}" if detail else ""),
            provider="xai-oauth",
            code="xai_refresh_failed",
            relogin_required=(response.status_code in {400, 401}),
        )
    try:
        payload = response.json()
    except Exception as exc:
        raise AuthError(
            f"xAI token refresh returned invalid JSON: {exc}",
            provider="xai-oauth",
            code="xai_refresh_invalid_json",
        ) from exc
    if not isinstance(payload, dict):
        raise AuthError(
            "xAI token refresh response was not a JSON object.",
            provider="xai-oauth",
            code="xai_refresh_invalid_response",
            relogin_required=True,
        )
    refreshed_access = str(payload.get("access_token", "") or "").strip()
    if not refreshed_access:
        raise AuthError(
            "xAI token refresh response was missing access_token.",
            provider="xai-oauth",
            code="xai_refresh_missing_access_token",
            relogin_required=True,
        )
    updated = {
        "access_token": refreshed_access,
        "refresh_token": str(payload.get("refresh_token") or refresh_token).strip(),
        "id_token": str(payload.get("id_token") or "").strip(),
        "expires_in": payload.get("expires_in"),
        "token_type": str(payload.get("token_type") or "Bearer").strip() or "Bearer",
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    return updated


def _refresh_xai_oauth_tokens(
    tokens: Dict[str, Any],
    *,
    token_endpoint: str,
    redirect_uri: str = "",
    timeout_seconds: float,
) -> Dict[str, Any]:
    refreshed = refresh_xai_oauth_pure(
        str(tokens.get("access_token", "") or ""),
        str(tokens.get("refresh_token", "") or ""),
        token_endpoint=token_endpoint,
        timeout_seconds=timeout_seconds,
    )
    updated_tokens = dict(tokens)
    updated_tokens["access_token"] = refreshed["access_token"]
    updated_tokens["refresh_token"] = refreshed["refresh_token"]
    if refreshed.get("id_token"):
        updated_tokens["id_token"] = refreshed["id_token"]
    if refreshed.get("expires_in") is not None:
        updated_tokens["expires_in"] = refreshed["expires_in"]
    if refreshed.get("token_type"):
        updated_tokens["token_type"] = refreshed["token_type"]
    _save_xai_oauth_tokens(
        updated_tokens,
        discovery={"token_endpoint": token_endpoint},
        redirect_uri=redirect_uri,
        last_refresh=refreshed["last_refresh"],
    )
    return updated_tokens


def resolve_xai_oauth_runtime_credentials(
    *,
    force_refresh: bool = False,
    refresh_if_expiring: bool = True,
    refresh_skew_seconds: int = XAI_ACCESS_TOKEN_REFRESH_SKEW_SECONDS,
) -> Dict[str, Any]:
    data = _read_xai_oauth_tokens()
    tokens = dict(data["tokens"])
    access_token = str(tokens.get("access_token", "") or "").strip()
    refresh_timeout_seconds = float(os.getenv("JANUS_XAI_REFRESH_TIMEOUT_SECONDS", "20"))
    discovery = dict(data.get("discovery") or {})
    token_endpoint = str(discovery.get("token_endpoint", "") or "").strip()
    redirect_uri = str(data.get("redirect_uri", "") or "").strip()

    should_refresh = bool(force_refresh)
    if (not should_refresh) and refresh_if_expiring:
        should_refresh = _xai_access_token_is_expiring(access_token, refresh_skew_seconds)
    if should_refresh:
        with _auth_store_lock(timeout_seconds=max(float(AUTH_LOCK_TIMEOUT_SECONDS), refresh_timeout_seconds + 5.0)):
            data = _read_xai_oauth_tokens(_lock=False)
            tokens = dict(data["tokens"])
            access_token = str(tokens.get("access_token", "") or "").strip()
            discovery = dict(data.get("discovery") or {})
            token_endpoint = str(discovery.get("token_endpoint", "") or "").strip()
            redirect_uri = str(data.get("redirect_uri", "") or "").strip()
            should_refresh = bool(force_refresh)
            if (not should_refresh) and refresh_if_expiring:
                should_refresh = _xai_access_token_is_expiring(access_token, refresh_skew_seconds)
            if should_refresh:
                if not token_endpoint:
                    token_endpoint = _xai_oauth_discovery(refresh_timeout_seconds)["token_endpoint"]
                try:
                    tokens = _refresh_xai_oauth_tokens(
                        tokens,
                        token_endpoint=token_endpoint,
                        redirect_uri=redirect_uri,
                        timeout_seconds=refresh_timeout_seconds,
                    )
                    access_token = str(tokens.get("access_token", "") or "").strip()
                except AuthError as exc:
                    if _is_terminal_xai_oauth_refresh_error(exc):
                        # Terminal failure (HTTP 400/401/403 — invalid_grant, token revoked).
                        # Clear dead tokens from auth.json so subsequent sessions fail fast
                        # without a network retry. Mirrors credential_pool.py quarantine.
                        try:
                            _q_store = _load_auth_store()
                            _q_state = _load_provider_state(_q_store, "xai-oauth") or {}
                            _q_tokens = dict(_q_state.get("tokens") or {})
                            _q_tokens.pop("access_token", None)
                            _q_tokens.pop("refresh_token", None)
                            _q_state["tokens"] = _q_tokens
                            _q_state["last_auth_error"] = {
                                "provider": "xai-oauth",
                                "code": exc.code or "xai_refresh_failed",
                                "message": str(exc),
                                "reason": "runtime_refresh_failure",
                                "relogin_required": True,
                                "at": datetime.now(timezone.utc).isoformat(),
                            }
                            _store_provider_state(_q_store, "xai-oauth", _q_state, set_active=False)
                            _save_auth_store(_q_store)
                        except Exception as _save_exc:
                            logger.debug(
                                "xAI OAuth: failed to persist quarantined state: %s", _save_exc,
                            )
                    raise

    base_url = _xai_validate_inference_base_url(
        os.getenv("JANUS_XAI_BASE_URL", "").strip().rstrip("/")
        or os.getenv("XAI_BASE_URL", "").strip().rstrip("/"),
        fallback=DEFAULT_XAI_OAUTH_BASE_URL,
    )
    return {
        "provider": "xai-oauth",
        "base_url": base_url,
        "api_key": access_token,
        "source": "janus-auth-store",
        "last_refresh": data.get("last_refresh"),
        "auth_mode": "oauth_pkce",
    }


def _is_terminal_xai_oauth_refresh_error(exc: Exception) -> bool:
    """True when retrying the same xAI OAuth refresh token cannot succeed.

    ``xai_refresh_failed`` covers HTTP 400/401/403 from the token endpoint
    (invalid_grant, token revoked, refresh_token_reused).
    ``xai_auth_missing_refresh_token`` means the pool entry has no refresh
    token at all — retrying will never work.
    Both carry ``relogin_required=True``; transient failures (429, 5xx) do not.
    """
    return (
        isinstance(exc, AuthError)
        and exc.provider == "xai-oauth"
        and exc.code in {"xai_refresh_failed", "xai_auth_missing_refresh_token"}
        and bool(exc.relogin_required)
    )


def _is_terminal_codex_oauth_refresh_error(exc: Exception) -> bool:
    """True when retrying the same Codex OAuth refresh token cannot succeed.

    ``codex_refresh_failed`` covers HTTP 400/401/403 from the token endpoint
    (invalid_grant, token revoked, refresh_token_reused).
    ``codex_auth_missing_refresh_token`` means the pool entry has no refresh
    token at all — retrying will never work.
    Both carry ``relogin_required=True``; transient failures (429, 5xx) do not.
    """
    return (
        isinstance(exc, AuthError)
        and exc.provider == "openai-codex"
        and exc.code in {
            "codex_refresh_failed",
            "codex_auth_missing_refresh_token",
            "invalid_grant",
            "invalid_token",
            "refresh_token_reused",
        }
        and bool(exc.relogin_required)
    )


# =============================================================================
# Status helpers
# =============================================================================


def get_codex_auth_status() -> Dict[str, Any]:
    """Status snapshot for Codex auth.
    
    Checks the credential pool first (where `janus auth` stores credentials),
    then falls back to the legacy provider state.
    """
    # Check credential pool first — this is where `janus auth` and
    # `janus model` store device_code tokens.
    try:
        from agent.credential_pool import load_pool
        pool = load_pool("openai-codex")
        if pool and pool.has_credentials():
            entry = pool.select()
            if entry is not None:
                api_key = (
                    getattr(entry, "runtime_api_key", None)
                    or getattr(entry, "access_token", "")
                )
                if api_key and not _codex_access_token_is_expiring(api_key, 0):
                    return {
                        "logged_in": True,
                        "auth_store": str(_auth_file_path()),
                        "last_refresh": getattr(entry, "last_refresh", None),
                        "auth_mode": "chatgpt",
                        "source": f"pool:{getattr(entry, 'label', 'unknown')}",
                        "api_key": api_key,
                    }
    except Exception:
        pass

    # Fall back to legacy provider state
    try:
        creds = resolve_codex_runtime_credentials()
        return {
            "logged_in": True,
            "auth_store": str(_auth_file_path()),
            "last_refresh": creds.get("last_refresh"),
            "auth_mode": creds.get("auth_mode"),
            "source": creds.get("source"),
            "api_key": creds.get("api_key"),
        }
    except AuthError as exc:
        return {
            "logged_in": False,
            "auth_store": str(_auth_file_path()),
            "error": str(exc),
        }


def get_xai_oauth_auth_status() -> Dict[str, Any]:
    try:
        from agent.credential_pool import load_pool

        pool = load_pool("xai-oauth")
        if pool and pool.has_credentials():
            entry = pool.select()
            if entry is not None:
                api_key = (
                    getattr(entry, "runtime_api_key", None)
                    or getattr(entry, "access_token", "")
                )
                if api_key and not _xai_access_token_is_expiring(api_key, 0):
                    return {
                        "logged_in": True,
                        "auth_store": str(_auth_file_path()),
                        "last_refresh": getattr(entry, "last_refresh", None),
                        "auth_mode": "oauth_pkce",
                        "source": f"pool:{getattr(entry, 'label', 'unknown')}",
                        "api_key": api_key,
                    }
    except Exception:
        pass

    try:
        creds = resolve_xai_oauth_runtime_credentials()
        return {
            "logged_in": True,
            "auth_store": str(_auth_file_path()),
            "last_refresh": creds.get("last_refresh"),
            "auth_mode": creds.get("auth_mode"),
            "source": creds.get("source"),
            "api_key": creds.get("api_key"),
        }
    except AuthError as exc:
        return {
            "logged_in": False,
            "auth_store": str(_auth_file_path()),
            "error": str(exc),
        }


def get_api_key_provider_status(provider_id: str) -> Dict[str, Any]:
    """Status snapshot for API-key providers (z.ai, Kimi, MiniMax)."""
    pconfig = PROVIDER_REGISTRY.get(provider_id)
    if not pconfig or pconfig.auth_type != "api_key":
        return {"configured": False}

    api_key = ""
    key_source = ""
    api_key, key_source = _resolve_api_key_provider_secret(provider_id, pconfig)

    env_url = ""
    if pconfig.base_url_env_var:
        env_url = os.getenv(pconfig.base_url_env_var, "").strip()

    if provider_id in {"kimi-coding", "kimi-coding-cn"}:
        base_url = _resolve_kimi_base_url(api_key, pconfig.inference_base_url, env_url)
    elif env_url:
        base_url = env_url
    else:
        base_url = pconfig.inference_base_url

    return {
        "configured": bool(api_key),
        "provider": provider_id,
        "name": pconfig.name,
        "key_source": key_source,
        "base_url": base_url,
        "logged_in": bool(api_key),  # compat with OAuth status shape
    }


def get_external_process_provider_status(provider_id: str) -> Dict[str, Any]:
    """Status snapshot for providers that run a local subprocess."""
    pconfig = PROVIDER_REGISTRY.get(provider_id)
    if not pconfig or pconfig.auth_type != "external_process":
        return {"configured": False}

    command = (
        os.getenv("JANUS_COPILOT_ACP_COMMAND", "").strip()
        or os.getenv("COPILOT_CLI_PATH", "").strip()
        or "copilot"
    )
    raw_args = os.getenv("JANUS_COPILOT_ACP_ARGS", "").strip()
    args = shlex.split(raw_args) if raw_args else ["--acp", "--stdio"]
    base_url = os.getenv(pconfig.base_url_env_var, "").strip() if pconfig.base_url_env_var else ""
    if not base_url:
        base_url = pconfig.inference_base_url

    resolved_command = shutil.which(command) if command else None
    return {
        "configured": bool(resolved_command or base_url.startswith("acp+tcp://")),
        "provider": provider_id,
        "name": pconfig.name,
        "command": command,
        "args": args,
        "resolved_command": resolved_command,
        "base_url": base_url,
        "logged_in": bool(resolved_command or base_url.startswith("acp+tcp://")),
    }


def get_auth_status(provider_id: Optional[str] = None) -> Dict[str, Any]:
    """Generic auth status dispatcher."""
    target = (provider_id or get_active_provider() or "").strip().lower()
    if not target:
        return {"logged_in": False}
    if target == "spotify":
        return get_spotify_auth_status()
    if target == "openai-codex":
        return get_codex_auth_status()
    if target == "xai-oauth":
        return get_xai_oauth_auth_status()
    if target == "qwen-oauth":
        return get_qwen_auth_status()
    if target == "google-gemini-cli":
        return get_gemini_oauth_auth_status()
    if target == "minimax-oauth":
        return get_minimax_oauth_auth_status()
    if target == "copilot-acp":
        return get_external_process_provider_status(target)
    if target == "azure-foundry":
        return _get_azure_foundry_auth_status()
    # API-key providers
    pconfig = PROVIDER_REGISTRY.get(target)
    if pconfig and pconfig.auth_type == "api_key":
        return get_api_key_provider_status(target)
    # AWS SDK providers (Bedrock) — check via boto3 credential chain
    if pconfig and pconfig.auth_type == "aws_sdk":
        try:
            from agent.bedrock_adapter import has_aws_credentials
            return {"logged_in": has_aws_credentials(), "provider": target}
        except ImportError:
            return {"logged_in": False, "provider": target, "error": "boto3 not installed"}
    return {"logged_in": False}


def _get_azure_foundry_auth_status() -> Dict[str, Any]:
    """Return structural auth status for Azure Foundry.

    ``logged_in`` is structural, matching other non-OAuth provider status
    checks:

      * ``auth_mode == "entra_id"`` AND ``azure-identity`` is importable
        (we do NOT mint a token here; ``janus doctor`` runs the live
        probe and reports whether the credential chain can acquire one).
      * ``auth_mode == "api_key"`` (default) AND ``AZURE_FOUNDRY_API_KEY``
        is set with a usable value.

    Never invokes the Entra credential chain — keeps CLI startup latency
    flat regardless of token-service / az login state.
    """
    info: Dict[str, Any] = {"provider": "azure-foundry"}
    try:
        from janus_cli.config import load_config, get_env_value
        cfg = load_config()
    except Exception:
        cfg = {}

    model_cfg = cfg.get("model") if isinstance(cfg, dict) else None
    auth_mode = "api_key"
    base_url = ""
    if isinstance(model_cfg, dict):
        auth_mode = str(model_cfg.get("auth_mode") or "api_key").strip().lower() or "api_key"
        base_url = str(model_cfg.get("base_url") or "").strip()
    info["auth_mode"] = auth_mode
    info["base_url"] = base_url

    if auth_mode == "entra_id":
        try:
            from agent.azure_identity_adapter import (
                EntraIdentityConfig,
                SCOPE_AI_AZURE_DEFAULT,
                has_azure_identity_installed,
            )
            installed = has_azure_identity_installed()
            entra_cfg = {}
            if isinstance(model_cfg, dict) and isinstance(model_cfg.get("entra"), dict):
                entra_cfg = model_cfg["entra"]
            identity_config = EntraIdentityConfig.from_dict(
                entra_cfg,
                default_scope=SCOPE_AI_AZURE_DEFAULT,
            )
            info["azure_identity_installed"] = installed
            info["scope"] = identity_config.scope
            info["credential_probe"] = "not_run"
            info["credential_verified"] = False
            info["logged_in"] = bool(installed)
            if not installed:
                info["hint"] = (
                    "azure-identity not installed. Install with: "
                    "pip install azure-identity  (or rely on Janus' "
                    "lazy-install at first use)."
                )
            else:
                info["hint"] = (
                    "azure-identity is installed; live credential validation "
                    "is skipped here. Run `janus doctor` to verify token acquisition."
                )
            return info
        except Exception as exc:
            info["logged_in"] = False
            info["error"] = f"azure-identity check failed: {exc}"
            return info

    # api_key mode (default)
    try:
        api_key = get_env_value("AZURE_FOUNDRY_API_KEY") or os.getenv("AZURE_FOUNDRY_API_KEY", "")
    except Exception:
        api_key = os.getenv("AZURE_FOUNDRY_API_KEY", "")
    info["logged_in"] = has_usable_secret(api_key)
    return info


def resolve_api_key_provider_credentials(provider_id: str) -> Dict[str, Any]:
    """Resolve API key and base URL for an API-key provider.

    Returns dict with: provider, api_key, base_url, source.
    """
    pconfig = PROVIDER_REGISTRY.get(provider_id)
    if not pconfig or pconfig.auth_type != "api_key":
        raise AuthError(
            f"Provider '{provider_id}' is not an API-key provider.",
            provider=provider_id,
            code="invalid_provider",
        )

    api_key = ""
    key_source = ""
    api_key, key_source = _resolve_api_key_provider_secret(provider_id, pconfig)

    # No-auth LM Studio: substitute a placeholder so runtime / auxiliary_client
    # see the local server as configured. doctor still reports unconfigured
    # because get_api_key_provider_status uses the raw secret resolver.
    if not api_key and provider_id == "lmstudio":
        api_key = LMSTUDIO_NOAUTH_PLACEHOLDER
        key_source = key_source or "default"

    env_url = ""
    if pconfig.base_url_env_var:
        env_url = os.getenv(pconfig.base_url_env_var, "").strip()

    if provider_id in {"kimi-coding", "kimi-coding-cn"}:
        base_url = _resolve_kimi_base_url(api_key, pconfig.inference_base_url, env_url)
    elif provider_id == "zai":
        base_url = _resolve_zai_base_url(api_key, pconfig.inference_base_url, env_url)
    elif env_url:
        base_url = env_url.rstrip("/")
    else:
        base_url = pconfig.inference_base_url

    return {
        "provider": provider_id,
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "source": key_source or "default",
    }


def resolve_external_process_provider_credentials(provider_id: str) -> Dict[str, Any]:
    """Resolve runtime details for local subprocess-backed providers."""
    pconfig = PROVIDER_REGISTRY.get(provider_id)
    if not pconfig or pconfig.auth_type != "external_process":
        raise AuthError(
            f"Provider '{provider_id}' is not an external-process provider.",
            provider=provider_id,
            code="invalid_provider",
        )

    base_url = os.getenv(pconfig.base_url_env_var, "").strip() if pconfig.base_url_env_var else ""
    if not base_url:
        base_url = pconfig.inference_base_url

    command = (
        os.getenv("JANUS_COPILOT_ACP_COMMAND", "").strip()
        or os.getenv("COPILOT_CLI_PATH", "").strip()
        or "copilot"
    )
    raw_args = os.getenv("JANUS_COPILOT_ACP_ARGS", "").strip()
    args = shlex.split(raw_args) if raw_args else ["--acp", "--stdio"]
    resolved_command = shutil.which(command) if command else None
    if not resolved_command and not base_url.startswith("acp+tcp://"):
        raise AuthError(
            f"Could not find the Copilot CLI command '{command}'. "
            "Install GitHub Copilot CLI or set JANUS_COPILOT_ACP_COMMAND/COPILOT_CLI_PATH.",
            provider=provider_id,
            code="missing_copilot_cli",
        )

    return {
        "provider": provider_id,
        "api_key": "copilot-acp",
        "base_url": base_url.rstrip("/"),
        "command": resolved_command or command,
        "args": args,
        "source": "process",
    }


# =============================================================================
# CLI Commands — login / logout
# =============================================================================

def _update_config_for_provider(
    provider_id: str,
    inference_base_url: str,
    default_model: Optional[str] = None,
) -> Path:
    """Update config.yaml and auth.json to reflect the active provider.

    When *default_model* is provided the function also writes it as the
    ``model.default`` value.  This prevents a race condition where the
    gateway (which re-reads config per-message) picks up the new provider
    before the caller has finished model selection, resulting in a
    mismatched model/provider (e.g. ``anthropic/claude-opus-4.6`` sent to
    MiniMax's API).
    """
    # Set active_provider in auth.json so auto-resolution picks this provider
    with _auth_store_lock():
        auth_store = _load_auth_store()
        auth_store["active_provider"] = provider_id
        _save_auth_store(auth_store)

    # Update config.yaml model section
    config_path = get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    config = read_raw_config()

    current_model = config.get("model")
    if isinstance(current_model, dict):
        model_cfg = dict(current_model)
    elif isinstance(current_model, str) and current_model.strip():
        model_cfg = {"default": current_model.strip()}
    else:
        model_cfg = {}

    model_cfg["provider"] = provider_id
    if inference_base_url and inference_base_url.strip():
        model_cfg["base_url"] = inference_base_url.rstrip("/")
    else:
        # Clear stale base_url to prevent contamination when switching providers
        model_cfg.pop("base_url", None)

    # Clear stale api_key/api_mode left over from a previous custom provider.
    # When the user switches from e.g. a MiniMax custom endpoint
    # (api_mode=anthropic_messages, api_key=mxp-...) to a built-in provider
    # (e.g. OpenRouter), the stale api_key/api_mode would override the new
    # provider's credentials and transport choice.  Built-in providers that
    # need a specific api_mode (copilot, xai) set it at request-resolution
    # time via `_copilot_runtime_api_mode` / `_detect_api_mode_for_url`, so
    # removing the persisted value here is safe.
    model_cfg.pop("api_key", None)
    model_cfg.pop("api_mode", None)

    # When switching to a non-OpenRouter provider, ensure model.default is
    # valid for the new provider.  An OpenRouter-formatted name like
    # "anthropic/claude-opus-4.6" will fail on direct-API providers.
    if default_model:
        cur_default = model_cfg.get("default", "")
        if not cur_default or "/" in cur_default:
            model_cfg["default"] = default_model

    config["model"] = model_cfg

    atomic_yaml_write(config_path, config, sort_keys=False)
    return config_path


def _get_config_provider() -> Optional[str]:
    """Return model.provider from config.yaml, normalized, if present."""
    try:
        config = read_raw_config()
    except Exception:
        return None
    if not config:
        return None
    model = config.get("model")
    if not isinstance(model, dict):
        return None
    provider = model.get("provider")
    if not isinstance(provider, str):
        return None
    provider = provider.strip().lower()
    return provider or None


def _config_provider_matches(provider_id: Optional[str]) -> bool:
    """Return True when config.yaml currently selects *provider_id*."""
    if not provider_id:
        return False
    return _get_config_provider() == provider_id.strip().lower()


def _should_reset_config_provider_on_logout(provider_id: Optional[str]) -> bool:
    """Return True when logout should reset the model provider config."""
    if not provider_id:
        return False
    normalized = provider_id.strip().lower()
    return normalized in PROVIDER_REGISTRY and _config_provider_matches(normalized)


def _logout_default_provider_from_config() -> Optional[str]:
    """Fallback logout target when auth.json has no active provider.

    `janus logout` historically keyed off auth.json.active_provider only.
    That left users stuck when auth state had already been cleared but
    config.yaml still selected an OAuth provider such as openai-codex for the
    agent model: there was no active auth provider to target, so logout printed
    "No provider is currently logged in" and never reset model.provider.
    """
    provider = _get_config_provider()
    if provider in {"openai-codex", "xai-oauth"}:
        return provider
    return None


def _reset_config_provider() -> Path:
    """Reset config.yaml provider back to auto after logout."""
    config_path = get_config_path()
    if not config_path.exists():
        return config_path

    config = read_raw_config()
    if not config:
        return config_path

    model = config.get("model")
    if isinstance(model, dict):
        model["provider"] = "auto"
        if "base_url" in model:
            model["base_url"] = OPENROUTER_BASE_URL
    atomic_yaml_write(config_path, config, sort_keys=False)
    return config_path


def _prompt_model_selection(
    model_ids: List[str],
    current_model: str = "",
    pricing: Optional[Dict[str, Dict[str, str]]] = None,
) -> Optional[str]:
    """Interactive model selection. Puts current_model first with a marker. Returns chosen model ID or None.

    If *pricing* is provided (``{model_id: {prompt, completion}}``), a compact
    price indicator is shown next to each model in aligned columns.
    """
    from janus_cli.models import _format_price_per_mtok

    # Reorder: current model first, then the rest (deduplicated)
    ordered = []
    if current_model and current_model in model_ids:
        ordered.append(current_model)
    for mid in model_ids:
        if mid not in ordered:
            ordered.append(mid)

    all_models = list(ordered)

    # Column-aligned labels when pricing is available
    has_pricing = bool(pricing and any(pricing.get(m) for m in all_models))
    name_col = max((len(m) for m in all_models), default=0) + 2 if has_pricing else 0

    # Pre-compute formatted prices and dynamic column widths
    _price_cache: dict[str, tuple[str, str, str]] = {}
    price_col = 3  # minimum width
    cache_col = 0  # only set if any model has cache pricing
    has_cache = False
    if has_pricing:
        for mid in all_models:
            p = pricing.get(mid)  # type: ignore[union-attr]
            if p:
                inp = _format_price_per_mtok(p.get("prompt", ""))
                out = _format_price_per_mtok(p.get("completion", ""))
                cache_read = p.get("input_cache_read", "")
                cache = _format_price_per_mtok(cache_read) if cache_read else ""
                if cache:
                    has_cache = True
            else:
                inp, out, cache = "", "", ""
            _price_cache[mid] = (inp, out, cache)
            price_col = max(price_col, len(inp), len(out))
            cache_col = max(cache_col, len(cache))
        if has_cache:
            cache_col = max(cache_col, 5)  # minimum: "Cache" header

    def _label(mid):
        if has_pricing:
            inp, out, cache = _price_cache.get(mid, ("", "", ""))
            price_part = f" {inp:>{price_col}}  {out:>{price_col}}"
            if has_cache:
                price_part += f"  {cache:>{cache_col}}"
            base = f"{mid:<{name_col}}{price_part}"
        else:
            base = mid
        if mid == current_model:
            base += "  ← currently in use"
        return base

    # Default cursor on the current model (index 0 if it was reordered to top)
    default_idx = 0

    # Build a pricing header hint for the menu title
    menu_title = "Select default model:"
    if has_pricing:
        # Align the header with the model column.
        # Each choice is "  {label}" (2 spaces) and simple_term_menu prepends
        # a 3-char cursor region ("-> " or "   "), so content starts at col 5.
        pad = " " * 5
        header = f"\n{pad}{'':>{name_col}} {'In':>{price_col}}  {'Out':>{price_col}}"
        if has_cache:
            header += f"  {'Cache':>{cache_col}}"
        menu_title += header + "  /Mtok"

    # Try arrow-key menu first, fall back to number input.
    # Uses the shared curses radiolist (ESC/arrow-key handling that works
    # across terminals, incl. those that emit raw escape sequences) instead
    # of simple_term_menu, which conflicts with /dev/tty and left ESC/arrow
    # keys unreliable in the setup model picker.
    try:
        from janus_cli.curses_ui import curses_radiolist

        choices = [_label(mid) for mid in ordered]
        choices.append("Enter custom model name")
        choices.append("Skip (keep current)")

        # The pricing column header is shown as a multi-line description above the list so it survives the curses
        # screen clear. menu_title already embeds the aligned price header.
        desc_lines: list[str] = []
        if has_pricing:
            # menu_title is "Select default model:\n<pad><header>  /Mtok"
            # Keep only the header portion for the description.
            header_part = menu_title.split("\n", 1)
            if len(header_part) > 1:
                desc_lines.extend(header_part[1].splitlines())
        description = "\n".join(desc_lines) if desc_lines else None

        idx = curses_radiolist(
            "Select default model:",
            choices,
            selected=default_idx,
            cancel_returns=-1,
            description=description,
            searchable=True,
        )
        if idx < 0:
            return None
        print()
        if idx < len(ordered):
            return ordered[idx]
        elif idx == len(ordered):
            try:
                custom = input("Enter model name: ").strip()
            except (EOFError, KeyboardInterrupt):
                return None
            return custom if custom else None
        return None
    except (ImportError, NotImplementedError, OSError, subprocess.SubprocessError):
        pass

    # Fallback: numbered list
    print(menu_title)
    num_width = len(str(len(ordered) + 2))
    for i, mid in enumerate(ordered, 1):
        print(f"  {i:>{num_width}}. {_label(mid)}")
    n = len(ordered)
    print(f"  {n + 1:>{num_width}}. Enter custom model name")
    print(f"  {n + 2:>{num_width}}. Skip (keep current)")
    print()

    while True:
        try:
            choice = input(f"Choice [1-{n + 2}] (default: skip): ").strip()
            if not choice:
                return None
            idx = int(choice)
            if 1 <= idx <= n:
                return ordered[idx - 1]
            elif idx == n + 1:
                custom = input("Enter model name: ").strip()
                return custom if custom else None
            elif idx == n + 2:
                return None
            print(f"Please enter 1-{n + 2}")
        except ValueError:
            print("Please enter a number")
        except (KeyboardInterrupt, EOFError):
            return None


def _save_model_choice(model_id: str) -> None:
    """Save the selected model to config.yaml (single source of truth).

    The model is stored in config.yaml only — NOT in .env.  This avoids
    conflicts in multi-agent setups where env vars would stomp each other.
    """
    from janus_cli.config import save_config, load_config

    config = load_config()
    # Always use dict format so provider/base_url can be stored alongside
    if isinstance(config.get("model"), dict):
        config["model"]["default"] = model_id
    else:
        config["model"] = {"default": model_id}
    save_config(config)


def _login_openai_codex(
    args,
    pconfig: ProviderConfig,
    *,
    force_new_login: bool = False,
) -> None:
    """OpenAI Codex login via device code flow. Tokens stored in ~/.janus/auth.json."""

    del args, pconfig  # kept for parity with other provider login helpers

    # Check for existing Janus-owned credentials
    if not force_new_login:
        try:
            existing = resolve_codex_runtime_credentials()
            # Verify the resolved token is actually usable (not expired).
            # resolve_codex_runtime_credentials attempts refresh, so if we get
            # here the token should be valid — but double-check before telling
            # the user "Login successful!".
            _resolved_key = existing.get("api_key", "")
            if isinstance(_resolved_key, str) and _resolved_key and not _codex_access_token_is_expiring(_resolved_key, 60):
                print("Existing Codex credentials found in Janus auth store.")
                try:
                    reuse = input("Use existing credentials? [Y/n]: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    reuse = "y"
                if reuse in {"", "y", "yes"}:
                    config_path = _update_config_for_provider("openai-codex", existing.get("base_url", DEFAULT_CODEX_BASE_URL))
                    print()
                    print("Login successful!")
                    print(f"  Config updated: {config_path} (model.provider=openai-codex)")
                    return
            else:
                print("Existing Codex credentials are expired. Starting fresh login...")
        except AuthError:
            pass

    # Check for existing Codex CLI tokens we can import
    if not force_new_login:
        cli_tokens = _import_codex_cli_tokens()
        if cli_tokens:
            print("Found existing Codex CLI credentials at ~/.codex/auth.json")
            print("Janus will create its own session to avoid conflicts with Codex CLI / VS Code.")
            try:
                do_import = input("Import these credentials? (a separate login is recommended) [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                do_import = "n"
            if do_import in {"y", "yes"}:
                _save_codex_tokens(cli_tokens)
                base_url = os.getenv("JANUS_CODEX_BASE_URL", "").strip().rstrip("/") or DEFAULT_CODEX_BASE_URL
                config_path = _update_config_for_provider("openai-codex", base_url)
                print()
                print("Credentials imported. Note: if Codex CLI refreshes its token,")
                print("Janus will keep working independently with its own session.")
                print(f"  Config updated: {config_path} (model.provider=openai-codex)")
                return

    # Run a fresh device code flow — Janus gets its own OAuth session
    print()
    print("Signing in to OpenAI Codex...")
    print("(Janus creates its own session — won't affect Codex CLI or VS Code)")
    print()

    creds = _codex_device_code_login()

    # Save tokens to Janus auth store
    _save_codex_tokens(creds["tokens"], creds.get("last_refresh"))
    config_path = _update_config_for_provider("openai-codex", creds.get("base_url", DEFAULT_CODEX_BASE_URL))
    print()
    print("Login successful!")
    from janus_constants import display_janus_home as _dhh
    print(f"  Auth state: {_dhh()}/auth.json")
    print(f"  Config updated: {config_path} (model.provider=openai-codex)")


def _login_xai_oauth(
    args,
    pconfig: ProviderConfig,
    *,
    force_new_login: bool = False,
) -> None:
    del pconfig

    if not force_new_login:
        try:
            existing = resolve_xai_oauth_runtime_credentials()
            api_key = existing.get("api_key", "")
            if isinstance(api_key, str) and api_key and not _xai_access_token_is_expiring(api_key, 60):
                print("Existing xAI OAuth credentials found in Janus auth store.")
                try:
                    reuse = input("Use existing credentials? [Y/n]: ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    reuse = "y"
                if reuse in {"", "y", "yes"}:
                    config_path = _update_config_for_provider(
                        "xai-oauth",
                        existing.get("base_url", DEFAULT_XAI_OAUTH_BASE_URL),
                    )
                    print()
                    print("Login successful!")
                    print(f"  Config updated: {config_path} (model.provider=xai-oauth)")
                    return
        except AuthError:
            pass

    print()
    print("Signing in to xAI Grok OAuth (SuperGrok / Premium+)...")
    print("(Janus creates its own local OAuth session)")
    print()

    timeout_seconds = float(getattr(args, "timeout", None) or 20.0)
    open_browser = not getattr(args, "no_browser", False)
    if _is_remote_session():
        open_browser = False
    manual_paste = bool(getattr(args, "manual_paste", False))

    creds = _xai_oauth_loopback_login(
        timeout_seconds=timeout_seconds,
        open_browser=open_browser,
        manual_paste=manual_paste,
    )
    _save_xai_oauth_tokens(
        creds["tokens"],
        discovery=creds.get("discovery"),
        redirect_uri=creds.get("redirect_uri", ""),
        last_refresh=creds.get("last_refresh"),
    )
    config_path = _update_config_for_provider("xai-oauth", creds.get("base_url", DEFAULT_XAI_OAUTH_BASE_URL))
    print()
    print("Login successful!")
    from janus_constants import display_janus_home as _dhh
    print(f"  Auth state: {_dhh()}/auth.json")
    print(f"  Config updated: {config_path} (model.provider=xai-oauth)")


def _xai_oauth_build_authorize_url(
    *,
    authorization_endpoint: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    nonce: str,
) -> str:
    # `plan=generic` opts the consent screen into xAI's generic OAuth plan
    # tier instead of falling back to the per-account default. Without it,
    # accounts.x.ai rejects loopback OAuth from non-allowlisted clients.
    # `referrer=janus-agent` lets xAI attribute Janus-originated logins
    # in their OAuth server logs (we still impersonate the upstream Grok-CLI
    # client_id; this is best-effort attribution until xAI mints us our own).
    authorize_params = {
        "response_type": "code",
        "client_id": XAI_OAUTH_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "scope": XAI_OAUTH_SCOPE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "nonce": nonce,
        "plan": "generic",
        "referrer": "janus-agent",
    }
    return f"{authorization_endpoint}?{urlencode(authorize_params)}"


def _xai_oauth_exchange_code_for_tokens(
    *,
    token_endpoint: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    code_challenge: str,
    timeout_seconds: float = 20.0,
) -> Dict[str, Any]:
    """POST the authorization code to xAI's token endpoint and return
    the parsed JSON payload.

    Sends ``code_verifier`` as required by RFC 7636 §4.5.  Also echoes
    ``code_challenge`` + ``code_challenge_method`` in the request body
    as a defense-in-depth measure for OAuth servers (xAI's among them,
    per #26990) that re-validate the challenge at the token step
    instead of relying solely on server-side session state captured
    during the authorize step.  Echoing the challenge is harmless for
    strict RFC-compliant servers — RFC 7636 doesn't forbid additional
    parameters at the token endpoint — and decisively fixes the
    ``code_challenge is required`` failure mode users hit on the
    loopback flow.

    Raises :class:`AuthError` on any non-2xx response or transport
    failure; the error message embeds the HTTP status code and the
    full response body so users can disambiguate cause at a glance.
    """
    # Paranoia: if upstream call sites ever drop ``code_verifier`` we
    # want to surface a precise, local error rather than send a
    # missing-PKCE request to xAI and receive their generic "code
    # challenge required" message back.
    if not code_verifier:
        raise AuthError(
            "xAI token exchange refused locally: PKCE code_verifier is empty. "
            "This is a bug in Janus — please report at "
            "https://github.com/magnetoid/janus/issues/26990.",
            provider="xai-oauth",
            code="xai_pkce_verifier_missing",
        )

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": XAI_OAUTH_CLIENT_ID,
        "code_verifier": code_verifier,
    }
    # Defense-in-depth: include the original ``code_challenge`` and
    # ``code_challenge_method``.  Some OAuth servers (including xAI's
    # auth.x.ai implementation, per the symptom reported in #26990)
    # validate these at the token endpoint instead of relying purely on
    # state captured during the authorize step — without them, xAI
    # rejects the exchange with ``code_challenge is required`` even
    # though we sent a valid ``code_verifier``.
    if code_challenge:
        data["code_challenge"] = code_challenge
        data["code_challenge_method"] = "S256"

    try:
        response = httpx.post(
            token_endpoint,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data=data,
            timeout=max(20.0, timeout_seconds),
        )
    except Exception as exc:
        raise AuthError(
            f"xAI token exchange failed: {exc}",
            provider="xai-oauth",
            code="xai_token_exchange_failed",
        ) from exc

    if response.status_code != 200:
        body = response.text.strip()
        # See ``refresh_xai_oauth_pure`` — token-exchange 403 also
        # surfaces tier/entitlement gating from xAI's backend.  Avoid
        # the misleading "re-authenticate" hint and point at the API
        # key fallback.  See #26847.
        if response.status_code == 403:
            raise AuthError(
                f"xAI token exchange failed (HTTP 403)."
                + (f" Response: {body}" if body else "")
                + " This OAuth account is not authorized for xAI API"
                  " access — xAI may be restricting API/OAuth use to"
                  " specific SuperGrok tiers despite the in-app"
                  " subscription being active. Set ``XAI_API_KEY``"
                  " and switch to ``provider: xai`` (API-key path) if"
                  " available, or upgrade your subscription at"
                  " https://x.ai/grok.",
                provider="xai-oauth",
                code="xai_oauth_tier_denied",
                relogin_required=False,
            )
        raise AuthError(
            f"xAI token exchange failed (HTTP {response.status_code})."
            + (f" Response: {body}" if body else ""),
            provider="xai-oauth",
            code="xai_token_exchange_failed",
        )

    try:
        payload = response.json()
    except Exception as exc:
        raise AuthError(
            f"xAI token exchange returned invalid JSON: {exc}",
            provider="xai-oauth",
            code="xai_token_exchange_invalid",
        ) from exc
    if not isinstance(payload, dict):
        raise AuthError(
            "xAI token exchange response was not a JSON object.",
            provider="xai-oauth",
            code="xai_token_exchange_invalid",
        )
    return payload


def _xai_oauth_loopback_login(
    *,
    timeout_seconds: float = 20.0,
    open_browser: bool = True,
    manual_paste: bool = False,
) -> Dict[str, Any]:
    """Run the xAI OAuth PKCE flow.

    When ``manual_paste=True`` the loopback HTTP listener is skipped
    entirely and the user is prompted to paste the failed callback
    URL into stdin (regression fix for #26923 — browser-only remote
    consoles like GCP Cloud Shell / GitHub Codespaces / EC2 Instance
    Connect, where the laptop's browser can't reach 127.0.0.1 on the
    remote VM).  The same PKCE verifier, ``state``, and ``nonce`` are
    used for both paths so the upstream-side OAuth flow is identical.
    """
    def _stdin_supports_manual_paste() -> bool:
        try:
            return bool(getattr(sys.stdin, "isatty", lambda: False)())
        except Exception:
            return False

    discovery = _xai_oauth_discovery(timeout_seconds)
    authorization_endpoint = discovery["authorization_endpoint"]
    token_endpoint = discovery["token_endpoint"]

    if manual_paste:
        # No HTTP listener — synthesize a redirect_uri matching what
        # the server would have bound to so the authorize URL the user
        # opens (and the redirect_uri sent in the token exchange) stay
        # byte-identical to the loopback path.  xAI's token endpoint
        # cross-checks redirect_uri against the authorize request.
        redirect_uri = (
            f"http://{XAI_OAUTH_REDIRECT_HOST}:{XAI_OAUTH_REDIRECT_PORT}"
            f"{XAI_OAUTH_REDIRECT_PATH}"
        )
        _xai_validate_loopback_redirect_uri(redirect_uri)
        code_verifier = _oauth_pkce_code_verifier()
        code_challenge = _oauth_pkce_code_challenge(code_verifier)
        state = uuid.uuid4().hex
        nonce = uuid.uuid4().hex
        authorize_url = _xai_oauth_build_authorize_url(
            authorization_endpoint=authorization_endpoint,
            redirect_uri=redirect_uri,
            code_challenge=code_challenge,
            state=state,
            nonce=nonce,
        )

        print("Open this URL to authorize Janus with xAI:")
        print(authorize_url)
        callback = _prompt_manual_callback_paste(redirect_uri)
    else:
        server, thread, callback_result, redirect_uri = _xai_start_callback_server()
        try:
            _xai_validate_loopback_redirect_uri(redirect_uri)
            code_verifier = _oauth_pkce_code_verifier()
            code_challenge = _oauth_pkce_code_challenge(code_verifier)
            state = uuid.uuid4().hex
            nonce = uuid.uuid4().hex
            authorize_url = _xai_oauth_build_authorize_url(
                authorization_endpoint=authorization_endpoint,
                redirect_uri=redirect_uri,
                code_challenge=code_challenge,
                state=state,
                nonce=nonce,
            )

            print("Open this URL to authorize Janus with xAI:")
            print(authorize_url)
            print()
            print(f"Waiting for callback on {redirect_uri}")

            _print_loopback_ssh_hint(redirect_uri, docs_url=XAI_OAUTH_DOCS_URL)

            if open_browser and not _is_remote_session() and _can_open_graphical_browser():
                try:
                    opened = webbrowser.open(authorize_url)
                except Exception:
                    opened = False
                if opened:
                    print("Browser opened for xAI authorization.")
                else:
                    print("Could not open the browser automatically; use the URL above.")

            try:
                callback = _xai_wait_for_callback(
                    server,
                    thread,
                    callback_result,
                    timeout_seconds=max(30.0, timeout_seconds * 9),
                )
            except AuthError as exc:
                if (
                    getattr(exc, "code", "") != "xai_callback_timeout"
                    or not _stdin_supports_manual_paste()
                ):
                    raise
                print()
                print("xAI loopback callback timed out.")
                print("If your browser reached a failed 127.0.0.1 callback page,")
                print("paste that FULL callback URL below to continue this login.")
                print("You can also re-run with `--manual-paste` to skip the")
                print("loopback listener from the start.")
                callback = _prompt_manual_callback_paste(redirect_uri)
                if callback.get("code") is None and callback.get("error") is None:
                    raise exc
        except Exception:
            try:
                server.shutdown()
                server.server_close()
            except Exception:
                pass
            try:
                thread.join(timeout=1.0)
            except Exception:
                pass
            raise

    if callback.get("error"):
        detail = callback.get("error_description") or callback["error"]
        raise AuthError(
            f"xAI authorization failed: {detail}",
            provider="xai-oauth",
            code="xai_authorization_failed",
        )
    callback_state = callback.get("state")
    # Manual-paste bare-code path: when a user pastes only the opaque
    # authorization code (no ``code=``/``state=`` query parameters),
    # ``_parse_pasted_callback`` returns ``state=None``.  xAI's consent
    # page renders the code in-page rather than redirecting through the
    # 127.0.0.1 callback, so on many remote setups (Cloud Shell, headless
    # VPS, container consoles) the bare code is the only thing the user
    # can obtain.  PKCE (code_verifier) still binds the exchange to this
    # client, so the local state-equality check is redundant on the
    # bare-code path — we substitute the locally generated state to keep
    # the rest of the validation chain (and the token exchange) unchanged.
    # See #26923 (AccursedGalaxy comment, 2026-05-20).
    if callback_state is None and manual_paste:
        callback_state = state
    if callback_state != state:
        raise AuthError(
            "xAI authorization failed: state mismatch.",
            provider="xai-oauth",
            code="xai_state_mismatch",
        )
    code = str(callback.get("code") or "").strip()
    if not code:
        raise AuthError(
            "xAI authorization failed: missing authorization code.",
            provider="xai-oauth",
            code="xai_code_missing",
        )

    payload = _xai_oauth_exchange_code_for_tokens(
        token_endpoint=token_endpoint,
        code=code,
        redirect_uri=redirect_uri,
        code_verifier=code_verifier,
        code_challenge=code_challenge,
        timeout_seconds=timeout_seconds,
    )
    access_token = str(payload.get("access_token", "") or "").strip()
    refresh_token = str(payload.get("refresh_token", "") or "").strip()
    if not access_token:
        raise AuthError(
            "xAI token exchange did not return an access_token.",
            provider="xai-oauth",
            code="xai_token_exchange_invalid",
        )
    if not refresh_token:
        raise AuthError(
            "xAI token exchange did not return a refresh_token.",
            provider="xai-oauth",
            code="xai_token_exchange_invalid",
        )

    base_url = _xai_validate_inference_base_url(
        os.getenv("JANUS_XAI_BASE_URL", "").strip().rstrip("/")
        or os.getenv("XAI_BASE_URL", "").strip().rstrip("/"),
        fallback=DEFAULT_XAI_OAUTH_BASE_URL,
    )
    return {
        "tokens": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": str(payload.get("id_token", "") or "").strip(),
            "expires_in": payload.get("expires_in"),
            "token_type": str(payload.get("token_type") or "Bearer").strip() or "Bearer",
        },
        "discovery": discovery,
        "redirect_uri": redirect_uri,
        "base_url": base_url,
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source": "oauth-loopback",
    }


def _codex_device_code_login() -> Dict[str, Any]:
    """Run the OpenAI device code login flow and return credentials dict."""
    import time as _time

    issuer = "https://auth.openai.com"
    client_id = CODEX_OAUTH_CLIENT_ID

    # Step 1: Request device code
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            resp = client.post(
                f"{issuer}/api/accounts/deviceauth/usercode",
                json={"client_id": client_id},
                headers={"Content-Type": "application/json"},
            )
    except Exception as exc:
        raise AuthError(
            f"Failed to request device code: {exc}",
            provider="openai-codex", code="device_code_request_failed",
        )

    if resp.status_code != 200:
        raise AuthError(
            f"Device code request returned status {resp.status_code}.",
            provider="openai-codex", code="device_code_request_error",
        )

    device_data = resp.json()
    user_code = device_data.get("user_code", "")
    device_auth_id = device_data.get("device_auth_id", "")
    poll_interval = max(3, int(device_data.get("interval", "5")))

    if not user_code or not device_auth_id:
        raise AuthError(
            "Device code response missing required fields.",
            provider="openai-codex", code="device_code_incomplete",
        )

    # Step 2: Show user the code
    print("To continue, follow these steps:\n")
    print("  1. Open this URL in your browser:")
    print(f"     \033[94m{issuer}/codex/device\033[0m\n")
    print("  2. Enter this code:")
    print(f"     \033[94m{user_code}\033[0m\n")
    print("Waiting for sign-in... (press Ctrl+C to cancel)")

    # Step 3: Poll for authorization code
    max_wait = 15 * 60  # 15 minutes
    start = _time.monotonic()
    code_resp = None

    try:
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            while _time.monotonic() - start < max_wait:
                _time.sleep(poll_interval)
                poll_resp = client.post(
                    f"{issuer}/api/accounts/deviceauth/token",
                    json={"device_auth_id": device_auth_id, "user_code": user_code},
                    headers={"Content-Type": "application/json"},
                )

                if poll_resp.status_code == 200:
                    code_resp = poll_resp.json()
                    break
                elif poll_resp.status_code in {403, 404}:
                    continue  # User hasn't completed login yet
                else:
                    raise AuthError(
                        f"Device auth polling returned status {poll_resp.status_code}.",
                        provider="openai-codex", code="device_code_poll_error",
                    )
    except KeyboardInterrupt:
        print("\nLogin cancelled.")
        raise SystemExit(130)

    if code_resp is None:
        raise AuthError(
            "Login timed out after 15 minutes.",
            provider="openai-codex", code="device_code_timeout",
        )

    # Step 4: Exchange authorization code for tokens
    authorization_code = code_resp.get("authorization_code", "")
    code_verifier = code_resp.get("code_verifier", "")
    redirect_uri = f"{issuer}/deviceauth/callback"

    if not authorization_code or not code_verifier:
        raise AuthError(
            "Device auth response missing authorization_code or code_verifier.",
            provider="openai-codex", code="device_code_incomplete_exchange",
        )

    try:
        with httpx.Client(timeout=httpx.Timeout(15.0)) as client:
            token_resp = client.post(
                CODEX_OAUTH_TOKEN_URL,
                data={
                    "grant_type": "authorization_code",
                    "code": authorization_code,
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                    "code_verifier": code_verifier,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
    except Exception as exc:
        raise AuthError(
            f"Token exchange failed: {exc}",
            provider="openai-codex", code="token_exchange_failed",
        )

    if token_resp.status_code != 200:
        raise AuthError(
            f"Token exchange returned status {token_resp.status_code}.",
            provider="openai-codex", code="token_exchange_error",
        )

    tokens = token_resp.json()
    access_token = tokens.get("access_token", "")
    refresh_token = tokens.get("refresh_token", "")

    if not access_token:
        raise AuthError(
            "Token exchange did not return an access_token.",
            provider="openai-codex", code="token_exchange_no_access_token",
        )

    # Return tokens for the caller to persist (no longer writes to ~/.codex/)
    base_url = (
        os.getenv("JANUS_CODEX_BASE_URL", "").strip().rstrip("/")
        or DEFAULT_CODEX_BASE_URL
    )

    return {
        "tokens": {
            "access_token": access_token,
            "refresh_token": refresh_token,
        },
        "base_url": base_url,
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "auth_mode": "chatgpt",
        "source": "device-code",
    }


# ==================== MiniMax Portal OAuth ====================

def _minimax_pkce_pair() -> tuple:
    """Generate (code_verifier, code_challenge_S256, state) for MiniMax OAuth."""
    import secrets
    verifier = secrets.token_urlsafe(64)[:96]
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).decode().rstrip("=")
    state = secrets.token_urlsafe(16)
    return verifier, challenge, state


def _minimax_request_user_code(
    client: httpx.Client, *, portal_base_url: str, client_id: str,
    code_challenge: str, state: str,
) -> Dict[str, Any]:
    response = client.post(
        f"{portal_base_url}/oauth/code",
        data={
            "response_type": "code",
            "client_id": client_id,
            "scope": MINIMAX_OAUTH_SCOPE,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "state": state,
        },
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "x-request-id": str(uuid.uuid4()),
        },
    )
    if response.status_code != 200:
        raise AuthError(
            f"MiniMax OAuth authorization failed: {response.text or response.reason_phrase}",
            provider="minimax-oauth", code="authorization_failed",
        )
    payload = response.json()
    for field_name in ("user_code", "verification_uri", "expired_in"):
        if field_name not in payload:
            raise AuthError(
                f"MiniMax OAuth response missing field: {field_name}",
                provider="minimax-oauth", code="authorization_incomplete",
            )
    if payload.get("state") != state:
        raise AuthError(
            "MiniMax OAuth state mismatch (possible CSRF).",
            provider="minimax-oauth", code="state_mismatch",
        )
    return payload


def _minimax_expired_in_looks_like_unix_ms(expired_in: int, *, now_ms: int) -> bool:
    """True if ``expired_in`` is plausibly a unix-ms absolute time (vs TTL seconds)."""
    return int(expired_in) > (now_ms // 2)


def _minimax_resolve_token_expiry_unix(expired_in: int, *, now: datetime) -> float:
    """Return access-token expiry as unix seconds (MiniMax uses ms epoch or TTL seconds)."""
    raw = int(expired_in)
    now_ms = int(now.timestamp() * 1000)
    if _minimax_expired_in_looks_like_unix_ms(raw, now_ms=now_ms):
        return raw / 1000.0
    return now.timestamp() + max(1, raw)


def _minimax_poll_token(
    client: httpx.Client, *, portal_base_url: str, client_id: str,
    user_code: str, code_verifier: str, expired_in: int, interval_ms: Optional[int],
) -> Dict[str, Any]:
    # OpenClaw treats expired_in as a unix-ms timestamp (Date.now() < expireTimeMs).
    # Defensive parsing: if it's small enough to be a duration, treat as seconds.
    import time as _time
    now_ms = int(_time.time() * 1000)
    raw = int(expired_in)
    if _minimax_expired_in_looks_like_unix_ms(raw, now_ms=now_ms):
        deadline = raw / 1000.0
    else:
        deadline = _time.time() + max(1, raw)
    interval = max(2.0, (interval_ms or 2000) / 1000.0)

    while _time.time() < deadline:
        response = client.post(
            f"{portal_base_url}/oauth/token",
            data={
                "grant_type": MINIMAX_OAUTH_GRANT_TYPE,
                "client_id": client_id,
                "user_code": user_code,
                "code_verifier": code_verifier,
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            payload = response.json() if response.text else {}
        except Exception:
            payload = {}

        if response.status_code != 200:
            msg = (payload.get("base_resp", {}) or {}).get("status_msg") or response.text
            raise AuthError(
                f"MiniMax OAuth error: {msg or 'unknown'}",
                provider="minimax-oauth", code="token_exchange_failed",
            )

        status = payload.get("status")
        if status == "error":
            raise AuthError(
                "MiniMax OAuth reported an error. Please try again later.",
                provider="minimax-oauth", code="authorization_denied",
            )
        if status == "success":
            if not all(payload.get(k) for k in ("access_token", "refresh_token", "expired_in")):
                raise AuthError(
                    "MiniMax OAuth success payload missing required token fields.",
                    provider="minimax-oauth", code="token_incomplete",
                )
            return payload
        # "pending" or any other status -> keep polling
        _time.sleep(interval)

    raise AuthError(
        "MiniMax OAuth timed out before authorization completed.",
        provider="minimax-oauth", code="timeout",
    )


def _minimax_save_auth_state(auth_state: Dict[str, Any]) -> None:
    """Persist MiniMax OAuth state to Janus auth store (~/.janus/auth.json)."""
    with _auth_store_lock():
        auth_store = _load_auth_store()
        _save_provider_state(auth_store, "minimax-oauth", auth_state)
        _save_auth_store(auth_store)


def _minimax_oauth_login(
    *, region: str = "global", open_browser: bool = True,
    timeout_seconds: float = 15.0,
) -> Dict[str, Any]:
    """Run MiniMax OAuth flow, persist tokens, return auth state dict."""
    pconfig = PROVIDER_REGISTRY["minimax-oauth"]
    if region == "cn":
        portal_base_url = pconfig.extra["cn_portal_base_url"]
        inference_base_url = pconfig.extra["cn_inference_base_url"]
    else:
        portal_base_url = pconfig.portal_base_url
        inference_base_url = pconfig.inference_base_url

    verifier, challenge, state = _minimax_pkce_pair()

    if _is_remote_session():
        open_browser = False

    print(f"Starting Janus login via MiniMax ({region}) OAuth...")
    print(f"Portal: {portal_base_url}")

    with httpx.Client(timeout=httpx.Timeout(timeout_seconds),
                      headers={"Accept": "application/json"},
                      follow_redirects=True) as client:
        code_data = _minimax_request_user_code(
            client, portal_base_url=portal_base_url,
            client_id=pconfig.client_id,
            code_challenge=challenge, state=state,
        )
        verification_url = str(code_data["verification_uri"])
        user_code = str(code_data["user_code"])

        print()
        print("To continue:")
        print(f"  1. Open: {verification_url}")
        print(f"  2. If prompted, enter code: {user_code}")
        if open_browser and _can_open_graphical_browser():
            if webbrowser.open(verification_url):
                print("  (Opened browser for verification)")
            else:
                print("  Could not open browser automatically -- use the URL above.")

        interval_raw = code_data.get("interval")
        interval_ms = int(interval_raw) if interval_raw is not None else None
        print("Waiting for approval...")

        token_data = _minimax_poll_token(
            client, portal_base_url=portal_base_url,
            client_id=pconfig.client_id,
            user_code=user_code, code_verifier=verifier,
            expired_in=int(code_data["expired_in"]),
            interval_ms=interval_ms,
        )

    now = datetime.now(timezone.utc)
    expires_at_unix = _minimax_resolve_token_expiry_unix(
        int(token_data["expired_in"]), now=now,
    )
    expires_in_s = max(0, int(expires_at_unix - now.timestamp()))

    auth_state = {
        "provider": "minimax-oauth",
        "region": region,
        "portal_base_url": portal_base_url,
        "inference_base_url": inference_base_url,
        "client_id": pconfig.client_id,
        "scope": MINIMAX_OAUTH_SCOPE,
        "token_type": token_data.get("token_type", "Bearer"),
        "access_token": token_data["access_token"],
        "refresh_token": token_data["refresh_token"],
        "resource_url": token_data.get("resource_url"),
        "obtained_at": now.isoformat(),
        "expires_at": datetime.fromtimestamp(expires_at_unix, tz=timezone.utc).isoformat(),
        "expires_in": expires_in_s,
    }

    _minimax_save_auth_state(auth_state)
    print("\u2713 MiniMax OAuth login successful.")
    if msg := token_data.get("notification_message"):
        print(f"Note from MiniMax: {msg}")
    return auth_state


def _refresh_minimax_oauth_state(
    state: Dict[str, Any], *, timeout_seconds: float = 15.0,
    force: bool = False,
) -> Dict[str, Any]:
    """Refresh MiniMax OAuth access token if close to expiry (or forced)."""
    if not state.get("refresh_token"):
        raise AuthError(
            "MiniMax OAuth state has no refresh_token; please re-login.",
            provider="minimax-oauth", code="no_refresh_token", relogin_required=True,
        )
    try:
        expires_at = datetime.fromisoformat(state.get("expires_at", "")).timestamp()
    except Exception:
        expires_at = 0.0
    now = time.time()
    if not force and (expires_at - now) > MINIMAX_OAUTH_REFRESH_SKEW_SECONDS:
        return state

    portal_base_url = state["portal_base_url"]
    with httpx.Client(timeout=httpx.Timeout(timeout_seconds),
                      follow_redirects=True) as client:
        response = client.post(
            f"{portal_base_url}/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": state["client_id"],
                "refresh_token": state["refresh_token"],
            },
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
    if response.status_code != 200:
        body = response.text.lower()
        relogin = any(m in body for m in
                      ("invalid_grant", "refresh_token_reused", "invalid_refresh_token"))
        raise AuthError(
            f"MiniMax OAuth refresh failed: {response.text or response.reason_phrase}",
            provider="minimax-oauth", code="refresh_failed",
            relogin_required=relogin,
        )
    payload = response.json()
    if payload.get("status") != "success":
        raise AuthError(
            "MiniMax OAuth refresh did not return success.",
            provider="minimax-oauth", code="refresh_failed",
            relogin_required=True,
        )
    now_dt = datetime.now(timezone.utc)
    expires_at_unix = _minimax_resolve_token_expiry_unix(
        int(payload["expired_in"]), now=now_dt,
    )
    expires_in_s = max(0, int(expires_at_unix - now_dt.timestamp()))
    new_state = dict(state)
    new_state.update({
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token", state["refresh_token"]),
        "obtained_at": now_dt.isoformat(),
        "expires_at": datetime.fromtimestamp(expires_at_unix, tz=timezone.utc).isoformat(),
        "expires_in": expires_in_s,
    })
    _minimax_save_auth_state(new_state)
    return new_state


def _minimax_oauth_quarantine_on_terminal_refresh(state: Dict[str, Any], exc: AuthError) -> None:
    """Wipe dead tokens from auth.json after a terminal refresh failure.

    Shared by both the eager-resolve path and the lazy per-request token
    provider. Mirrors the xAI-OAuth / Codex-OAuth quarantine pattern
    so subsequent calls fail fast without a network retry.
    """
    if not (exc.relogin_required and state.get("refresh_token")):
        return
    for _k in ("access_token", "refresh_token", "expires_at", "expires_in", "obtained_at"):
        state.pop(_k, None)
    state["last_auth_error"] = {
        "provider": "minimax-oauth",
        "code": exc.code or "refresh_failed",
        "message": str(exc),
        "reason": "runtime_refresh_failure",
        "relogin_required": True,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        _minimax_save_auth_state(state)
    except Exception as _save_exc:
        logger.debug("MiniMax OAuth: failed to persist quarantined state: %s", _save_exc)


def build_minimax_oauth_token_provider() -> Callable[[], str]:
    """Return a zero-arg callable that yields a fresh MiniMax access token.

    The Anthropic SDK caches ``api_key`` as a static string at construction
    time, so a session that resolves credentials once at startup will keep
    sending the same bearer until MiniMax's server returns 401 — typically
    ~15 minutes in, because MiniMax issues short-lived access tokens.

    Returning a *callable* instead of a string lets us hook into the
    existing Entra-ID bearer infrastructure in
    :mod:`agent.anthropic_adapter`: ``build_anthropic_client`` detects a
    callable and routes through ``_build_anthropic_client_with_bearer_hook``,
    which mints a fresh ``Authorization`` header on every outbound request.
    Each invocation re-reads the persisted state from ``auth.json`` and
    calls :func:`_refresh_minimax_oauth_state` — that helper is a no-op
    when the token still has more than ``MINIMAX_OAUTH_REFRESH_SKEW_SECONDS``
    of life left, so the steady-state cost is one file read + one
    timestamp compare per request.

    Reading state fresh each time also means a refresh persisted by one
    process (CLI, gateway, cron) is immediately visible to every other
    process sharing the same ``auth.json``.
    """
    def _provide() -> str:
        state = get_provider_auth_state("minimax-oauth")
        if not state or not state.get("access_token"):
            raise AuthError(
                "Not logged into MiniMax OAuth. Run `janus model` and select "
                "MiniMax (OAuth).",
                provider="minimax-oauth", code="not_logged_in", relogin_required=True,
            )
        try:
            state = _refresh_minimax_oauth_state(state)
        except AuthError as exc:
            _minimax_oauth_quarantine_on_terminal_refresh(state, exc)
            raise
        token = state.get("access_token")
        if not token:
            raise AuthError(
                "MiniMax OAuth state has no access_token after refresh.",
                provider="minimax-oauth", code="no_access_token", relogin_required=True,
            )
        return token

    return _provide


def resolve_minimax_oauth_runtime_credentials(
    *, min_token_ttl_seconds: int = MINIMAX_OAUTH_REFRESH_SKEW_SECONDS,
    as_token_provider: bool = False,
) -> Dict[str, Any]:
    """Return {provider, api_key, base_url, source} for minimax-oauth.

    When ``as_token_provider`` is True, ``api_key`` is a zero-arg callable
    that mints a fresh access token per call (proactively refreshing if
    the cached token is within ``MINIMAX_OAUTH_REFRESH_SKEW_SECONDS`` of
    expiry). This is what the runtime provider path uses so that long
    sessions survive MiniMax's short access-token lifetime — see
    :func:`build_minimax_oauth_token_provider` for the rationale.

    The default (string ``api_key``) preserves the historical contract for
    diagnostic call sites like ``janus status`` that just want to know
    whether a valid token exists right now.
    """
    state = get_provider_auth_state("minimax-oauth")
    if not state or not state.get("access_token"):
        raise AuthError(
            "Not logged into MiniMax OAuth. Run `janus model` and select "
            "MiniMax (OAuth).",
            provider="minimax-oauth", code="not_logged_in", relogin_required=True,
        )
    try:
        state = _refresh_minimax_oauth_state(state)
    except AuthError as exc:
        _minimax_oauth_quarantine_on_terminal_refresh(state, exc)
        raise
    if as_token_provider:
        api_key: Any = build_minimax_oauth_token_provider()
    else:
        api_key = state["access_token"]
    return {
        "provider": "minimax-oauth",
        "api_key": api_key,
        "base_url": state["inference_base_url"].rstrip("/"),
        "source": "oauth",
    }


def get_minimax_oauth_auth_status() -> Dict[str, Any]:
    """Return auth status dict for MiniMax OAuth provider."""
    state = get_provider_auth_state("minimax-oauth")
    if not state or not state.get("access_token"):
        return {"logged_in": False, "provider": "minimax-oauth"}
    try:
        expires_at = datetime.fromisoformat(state.get("expires_at", "")).timestamp()
        token_valid = (expires_at - time.time()) > 0
    except Exception:
        token_valid = bool(state.get("access_token"))
    return {
        "logged_in": token_valid,
        "provider": "minimax-oauth",
        "region": state.get("region", "global"),
        "expires_at": state.get("expires_at"),
    }


def _login_minimax_oauth(args, pconfig: ProviderConfig) -> None:
    """CLI entry for MiniMax OAuth login."""
    region = getattr(args, "region", None) or "global"
    open_browser = not getattr(args, "no_browser", False)
    timeout = getattr(args, "timeout", None) or 15.0
    try:
        _minimax_oauth_login(
            region=region, open_browser=open_browser, timeout_seconds=timeout,
        )
    except AuthError as exc:
        print(format_auth_error(exc))
        raise SystemExit(1)


def logout_command(args) -> None:
    """Clear auth state for a provider."""
    provider_id = getattr(args, "provider", None)

    if provider_id and not is_known_auth_provider(provider_id):
        print(f"Unknown provider: {provider_id}")
        raise SystemExit(1)

    active = get_active_provider()
    target = provider_id or active or _logout_default_provider_from_config()

    if not target:
        print("No provider is currently logged in.")
        return

    should_reset_config = _should_reset_config_provider_on_logout(target)
    provider_name = get_auth_provider_display_name(target)

    if clear_provider_auth(target) or should_reset_config:
        if should_reset_config:
            _reset_config_provider()
        print(f"Logged out of {provider_name}.")
        if should_reset_config and os.getenv("OPENROUTER_API_KEY"):
            print("Janus will use OpenRouter for inference.")
        elif should_reset_config:
            print("Run `janus model` or configure an API key to use Janus.")
        else:
            print("Model provider configuration was unchanged.")
    else:
        print(f"No auth state found for {provider_name}.")
