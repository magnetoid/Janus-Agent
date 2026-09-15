"""The parts of ``GET``/``PUT /v1/config`` that are not HTTP.

A host that runs Janus as a service (Blob's Compose stack) needs to show a
settings page: read the effective configuration, write a change, restart.
Everything here is synchronous and side-effect-explicit so the adapter's two
handlers stay thin — auth, JSON in, call, JSON out — and so the interesting
parts are testable without a socket.

Two rules the code exists to keep:

* **A key's value appears in no response and no log line, ever.** Presence and
  the last four characters are the whole of what ``GET`` says about a key, and
  ``PUT`` echoes names only. :func:`provider_models` scrubs the resolved key
  out of any failure reason before returning it.
* **The operator's file stays the operator's file.** A merge reads
  ``config.yaml`` with ``yaml.safe_load`` — never ``load_config()``, which
  would dump every default Janus has into a file the operator wrote by hand.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from janus_cli.config import (
    _CONFIG_LOCK,
    _reject_denylisted_env_var,
    _set_nested,
    get_config_path,
    get_env_value,
    is_managed,
    load_config,
    remove_env_value,
    save_env_value,
    validate_config_structure,
)
from utils import atomic_text_write, atomic_yaml_write

logger = logging.getLogger(__name__)

__all__ = [
    "AGENT_KEYS",
    "KEY_NAME_RE",
    "MODEL_KEYS",
    "ConfigChangeError",
    "apply_change",
    "drain_timeout_seconds",
    "is_managed",
    "provider_models",
    "read_view",
    "restart_requested",
]

# The platform whose toolset selection this route reads and writes. The API
# server's tools are ``platform_toolsets.api_server`` — the root ``toolsets:``
# key is the CLI's own list and is NOT what ``_get_platform_tools`` consults
# for a platform, so writing it would report success and change nothing.
# ``janus tools`` writes the same per-platform key.
PLATFORM = "api_server"

# An API key or token, and nothing else. The outer gate on ``api_keys``:
# ``_reject_denylisted_env_var`` runs behind it, but no denylisted name
# (PATH, PYTHONPATH, JANUS_HOME, ...) can match this in the first place.
KEY_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*_(API_KEY|TOKEN)$")

MODEL_KEYS = ("default", "provider", "base_url")
AGENT_KEYS = ("max_turns", "reasoning_effort", "gateway_timeout", "personality")

# Where each writable key lives in config.yaml. ``agent.personality`` is the
# odd one out: the config names the default personality under ``display``
# (``personalities`` at the root is the catalogue it picks from), and the API
# presents it beside the other agent settings because that is where a person
# looks for it.
_AGENT_PATHS = {
    "max_turns": "agent.max_turns",
    "reasoning_effort": "agent.reasoning_effort",
    "gateway_timeout": "agent.gateway_timeout",
    "personality": "display.personality",
}

_CHANGE_KEYS = ("model", "agent", "toolsets", "api_keys", "raw")

# What each writable value has to be. A settings page that posts "60" from a
# text input gets told so, rather than writing a string into a numeric key and
# breaking the next start — the failure would land far from its cause.
_INT_KEYS = frozenset({"max_turns", "gateway_timeout"})

MODELS_FETCH_TIMEOUT_SECONDS = 10.0

# The spellings ``_coerce_request_bool`` in api_server.py accepts, because a
# client that writes "false" means it. Anything outside this vocabulary is
# refused rather than defaulted: silently restarting the gateway because a flag
# was misspelled is the one wrong answer here.
_TRUE_RESTART_STRINGS = frozenset({"1", "true", "yes", "on"})
_FALSE_RESTART_STRINGS = frozenset({"0", "false", "no", "off"})


class ConfigChangeError(Exception):
    """A refused ``PUT`` — carries the HTTP status and the body to send."""

    def __init__(self, status: int, payload: Dict[str, Any]):
        super().__init__(payload.get("error", "config change refused"))
        self.status = status
        self.payload = payload


def _issue_dicts(issues) -> List[Dict[str, str]]:
    return [
        {"severity": issue.severity, "message": issue.message, "hint": issue.hint}
        for issue in issues
    ]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def _model_view(config: Dict[str, Any]) -> Dict[str, str]:
    """Normalise ``model`` into three strings.

    ``model:`` is a mapping in every config Janus writes, but the legacy
    scalar form (``model: some-name``) is still on disk in the wild and
    ``load_config`` leaves it alone.
    """
    raw = config.get("model")
    if isinstance(raw, dict):
        return {key: str(raw.get(key) or "") for key in MODEL_KEYS}
    return {"default": str(raw or ""), "provider": "", "base_url": ""}


def _agent_view(config: Dict[str, Any]) -> Dict[str, Any]:
    agent = config.get("agent")
    agent = agent if isinstance(agent, dict) else {}
    display = config.get("display")
    display = display if isinstance(display, dict) else {}
    personality = str(display.get("personality") or "").strip()
    return {
        "max_turns": agent.get("max_turns"),
        "reasoning_effort": str(agent.get("reasoning_effort") or ""),
        "gateway_timeout": agent.get("gateway_timeout"),
        "personality": personality or None,
    }


def _personality_names(config: Dict[str, Any]) -> List[str]:
    personalities = config.get("personalities")
    if not isinstance(personalities, dict):
        return []
    return sorted(str(name) for name in personalities)


def _toolsets_view(config: Dict[str, Any]) -> Dict[str, Any]:
    """What ``/v1/toolsets`` reports, reduced to two name lists.

    A failure is reported rather than swallowed: empty lists with no
    explanation read as "this Janus has no tools", which is a different and
    much more alarming thing than "I could not enumerate them".
    """
    try:
        from janus_cli.tools_config import (
            _get_effective_configurable_toolsets,
            _get_platform_tools,
        )

        available = sorted({str(name) for name, _label, _desc in _get_effective_configurable_toolsets()})
        enabled = sorted(
            {
                str(name)
                for name in _get_platform_tools(
                    config, PLATFORM, include_default_mcp_servers=False
                )
            }
        )
        return {"available": available, "enabled": enabled, "error": None}
    except Exception as exc:
        logger.debug("toolset enumeration failed: %s", exc)
        return {
            "available": [],
            "enabled": [],
            "error": _scrub(f"{type(exc).__name__}: {exc}", ""),
        }


def _providers_view() -> List[Dict[str, Any]]:
    """Every API-key provider, and whether its key is present — never its value."""
    from janus_cli.auth import PROVIDER_REGISTRY

    out: List[Dict[str, Any]] = []
    # The registry maps alias keys onto the same ProviderConfig (``novita``,
    # ``novita-ai`` and ``novitaai`` all carry id "novita"), so iterate its
    # values and keep the first of each id — a settings page showing one
    # provider three times is a bug the registry hands you for free.
    seen: set[str] = set()
    for pconfig in PROVIDER_REGISTRY.values():
        if pconfig.auth_type != "api_key" or not pconfig.api_key_env_vars:
            continue
        if pconfig.id in seen:
            continue
        seen.add(pconfig.id)
        env_name = pconfig.api_key_env_vars[0]
        value = os.environ.get(env_name) or get_env_value(env_name) or ""
        key: Dict[str, Any] = {"set": bool(value)}
        # A tail is only a tail when there is more key than tail — otherwise
        # "the last four characters" would be the whole secret.
        if value and len(value) > 4:
            key["tail"] = value[-4:]
        out.append(
            {
                "id": pconfig.id,
                "name": pconfig.name,
                "env": env_name,
                "key": key,
            }
        )
    return out


def _models_view(
    provider_id: str, fetch_models: Optional[Callable[..., Tuple[Optional[List[str]], Optional[str]]]]
) -> Dict[str, Any]:
    """The configured provider's own model list, fetched live or explained away."""
    from janus_cli.auth import PROVIDER_REGISTRY

    if not provider_id:
        return {"provider": None, "ids": None, "reason": "no provider configured"}

    pconfig = PROVIDER_REGISTRY.get(provider_id)
    if pconfig is None:
        return {
            "provider": provider_id,
            "ids": None,
            "reason": f"unknown provider '{provider_id}'",
        }
    if pconfig.auth_type != "api_key":
        return {
            "provider": provider_id,
            "ids": None,
            "reason": f"provider '{provider_id}' is not an API-key provider",
        }

    fetch = fetch_models or provider_models
    ids, reason = fetch(provider_id, timeout=MODELS_FETCH_TIMEOUT_SECONDS)
    return {"provider": provider_id, "ids": ids, "reason": reason}


def read_view(
    *,
    version: str,
    restart_pending: bool,
    fetch_models: Optional[Callable[..., Tuple[Optional[List[str]], Optional[str]]]] = None,
) -> Dict[str, Any]:
    """The whole ``GET /v1/config`` body.

    ``model`` and ``agent`` are the *effective* configuration, so a value Janus
    is running on shows even when the file does not name it; ``raw`` is the
    file's own text, so an editor round-trips what the operator wrote.
    """
    config = load_config()
    model = _model_view(config)
    config_path = get_config_path()
    try:
        raw = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    except OSError as exc:
        logger.debug("config.yaml unreadable: %s", exc)
        raw = ""

    return {
        "object": "janus.config",
        "version": version,
        "model": model,
        "agent": _agent_view(config),
        "personalities": _personality_names(config),
        "toolsets": _toolsets_view(config),
        "providers": _providers_view(),
        "models": _models_view(model["provider"], fetch_models),
        "raw": raw,
        "restart_pending": bool(restart_pending),
    }


def provider_models(
    provider_id: str, *, timeout: float = MODELS_FETCH_TIMEOUT_SECONDS
) -> Tuple[Optional[List[str]], Optional[str]]:
    """``GET {base_url}/models`` for an API-key provider, sorted.

    Returns ``(ids, None)`` or ``(None, reason)`` — never raises, never caches:
    the point of asking the provider is that a retired model name disappears
    from the list. The reason is one line with the key scrubbed out of it.
    """
    api_key = ""
    try:
        import httpx

        from janus_cli.auth import resolve_api_key_provider_credentials

        creds = resolve_api_key_provider_credentials(provider_id)
        api_key = str(creds.get("api_key") or "")
        base_url = str(creds.get("base_url") or "").rstrip("/")
        if not api_key:
            return None, f"no API key set for provider '{provider_id}'"
        if not base_url:
            return None, f"no base URL for provider '{provider_id}'"

        with httpx.Client(timeout=timeout, headers={"Accept": "application/json"}) as client:
            response = client.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        status = getattr(response, "status_code", 0)
        if status != 200:
            return None, f"/models returned HTTP {status}"

        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            return None, "/models returned no data list"

        ids = sorted(
            {
                str(item["id"])
                for item in data
                if isinstance(item, dict) and item.get("id")
            }
        )
        return ids, None
    except Exception as exc:
        return None, _scrub(f"{type(exc).__name__}: {exc}", api_key)


def _scrub(text: str, secret: str) -> str:
    """One line, with the key taken out of it."""
    line = " ".join(text.split())
    if secret and secret in line:
        line = line.replace(secret, "***")
    return line


def restart_requested(body: Dict[str, Any]) -> bool:
    """Whether this change asks for the gateway to restart. Default: yes.

    Refuses anything that is not a boolean or one of the true/false spellings a
    JSON-shy client might send. ``{"restart": "maybe"}`` used to be truthy, and
    a typo that restarts the gateway is not an acceptable failure mode.
    """
    value = body.get("restart", True)
    if isinstance(value, bool):
        return value
    if value is None:
        return True
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_RESTART_STRINGS:
            return True
        if normalized in _FALSE_RESTART_STRINGS:
            return False
    raise ConfigChangeError(400, {"error": "restart must be true or false"})


def drain_timeout_seconds() -> float:
    """How long a restart waits for in-flight runs — what the caller will see."""
    from gateway.restart import parse_restart_drain_timeout

    agent = load_config().get("agent")
    raw = agent.get("restart_drain_timeout") if isinstance(agent, dict) else None
    return parse_restart_drain_timeout(raw)


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _validate_shape(body: Any) -> None:
    """Refuse a body that cannot mean anything, before anything is written."""
    if not isinstance(body, dict):
        raise ConfigChangeError(400, {"error": "body must be a JSON object"})
    if not any(key in body for key in _CHANGE_KEYS):
        raise ConfigChangeError(400, {"error": "empty body"})

    # Raises for an unusable value, so a misspelled restart flag refuses the
    # whole request instead of writing the change and then restarting anyway.
    restart_requested(body)

    if "raw" in body and any(key in body for key in ("model", "agent", "toolsets")):
        raise ConfigChangeError(
            400,
            {"error": "raw cannot be combined with model, agent or toolsets"},
        )
    if "raw" in body and not isinstance(body["raw"], str):
        raise ConfigChangeError(400, {"error": "raw must be a string"})

    for section, allowed in (("model", MODEL_KEYS), ("agent", AGENT_KEYS)):
        if section not in body:
            continue
        given = body[section]
        if not isinstance(given, dict):
            raise ConfigChangeError(400, {"error": f"{section} must be an object"})
        unknown = [key for key in given if key not in allowed]
        if unknown:
            raise ConfigChangeError(
                400,
                {
                    "error": (
                        f"unknown {section} key(s): {', '.join(sorted(unknown))} "
                        f"— allowed: {', '.join(allowed)}"
                    )
                },
            )
        for key, value in given.items():
            if value is None:
                continue
            if key in _INT_KEYS:
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise ConfigChangeError(
                        400,
                        {"error": f"{section}.{key} must be a non-negative whole number"},
                    )
            elif not isinstance(value, str):
                raise ConfigChangeError(400, {"error": f"{section}.{key} must be a string"})

    if "toolsets" in body:
        toolsets = body["toolsets"]
        if not isinstance(toolsets, list) or not all(isinstance(t, str) for t in toolsets):
            raise ConfigChangeError(400, {"error": "toolsets must be a list of strings"})

    if "api_keys" in body:
        api_keys = body["api_keys"]
        if not isinstance(api_keys, dict):
            raise ConfigChangeError(400, {"error": "api_keys must be an object"})
        for name, value in api_keys.items():
            if not isinstance(name, str) or not KEY_NAME_RE.match(name):
                raise ConfigChangeError(
                    400,
                    {"error": f"{name!r} is not an API key name (expected e.g. DEEPSEEK_API_KEY)"},
                )
            try:
                _reject_denylisted_env_var(name)
            except ValueError as exc:
                raise ConfigChangeError(400, {"error": f"{name}: {exc}"}) from exc
            if not isinstance(value, str):
                raise ConfigChangeError(400, {"error": f"{name}: value must be a string"})
            try:
                value.encode("ascii")
            except UnicodeEncodeError as exc:
                # Refused rather than silently stripped. ``save_env_value``
                # would strip the non-ASCII characters and print them to
                # stderr — which both breaks the no-key-in-any-log rule and
                # saves a key that is not the one the operator pasted. The
                # message names the key, never its value.
                raise ConfigChangeError(
                    400,
                    {
                        "error": (
                            f"{name}: value must be ASCII — a copy-paste from a PDF or "
                            "rich-text editor can substitute lookalike characters"
                        )
                    },
                ) from exc


def _validated_mapping(mapping: Dict[str, Any]) -> List[Dict[str, str]]:
    """Refuse a mapping that would break Janus; return the warnings to report."""
    issues = validate_config_structure(mapping)
    errors = [issue for issue in issues if issue.severity == "error"]
    if errors:
        raise ConfigChangeError(
            400, {"error": "invalid config", "issues": _issue_dicts(issues)}
        )
    return _issue_dicts([issue for issue in issues if issue.severity != "error"])


def _current_user_mapping() -> Dict[str, Any]:
    """``config.yaml`` as the operator wrote it — no defaults merged in."""
    path = get_config_path()
    if not path.exists():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigChangeError(
            400, {"error": f"config.yaml could not be parsed: {_scrub(str(exc), '')}"}
        ) from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigChangeError(400, {"error": "config.yaml is not a mapping"})
    return loaded


def apply_change(body: Dict[str, Any]) -> Tuple[Dict[str, Any], List[Dict[str, str]]]:
    """Validate and write a ``PUT /v1/config`` body.

    Returns ``(applied, warnings)``; raises :class:`ConfigChangeError` for
    anything refused. Nothing is written until everything validates, so a
    rejected change leaves the file exactly as it was.
    """
    _validate_shape(body)

    applied: Dict[str, Any] = {
        "model": {},
        "agent": {},
        "toolsets": None,
        "api_keys": [],
        "raw": False,
    }
    warnings: List[Dict[str, str]] = []

    with _CONFIG_LOCK:
        if "raw" in body:
            try:
                loaded = yaml.safe_load(body["raw"])
            except yaml.YAMLError as exc:
                raise ConfigChangeError(
                    400, {"error": f"raw is not valid YAML: {_scrub(str(exc), '')}"}
                ) from exc
            if loaded is None:
                loaded = {}
            if not isinstance(loaded, dict):
                raise ConfigChangeError(400, {"error": "raw must be a YAML mapping"})
            warnings.extend(_validated_mapping(loaded))
            # The parse proved the text is a valid mapping; the *text* is what
            # gets written. Dumping ``loaded`` back out would cost the
            # operator every comment and every deliberate bit of formatting,
            # and GET's ``raw`` would not return what was just saved.
            atomic_text_write(get_config_path(), body["raw"])
            applied["raw"] = True

        elif any(key in body for key in ("model", "agent", "toolsets")):
            mapping = _current_user_mapping()

            model_change = {
                key: value for key, value in (body.get("model") or {}).items() if value is not None
            }
            if model_change:
                existing_model = mapping.get("model")
                if existing_model and not isinstance(existing_model, dict):
                    # Legacy scalar form: ``model: some-name`` is the default
                    # model's name. ``_set_nested`` replaces a scalar leaf with
                    # a fresh dict, so without this a partial merge
                    # (``{"model": {"provider": "x"}}``) would silently drop
                    # the model Janus is running on.
                    mapping["model"] = {"default": existing_model}
            for key, value in model_change.items():
                _set_nested(mapping, f"model.{key}", value)
                applied["model"][key] = value

            for key, value in (body.get("agent") or {}).items():
                if value is None:
                    continue
                _set_nested(mapping, _AGENT_PATHS[key], value)
                applied["agent"][key] = value

            if "toolsets" in body:
                # ``platform_toolsets.api_server``, not the root ``toolsets:``
                # key — see PLATFORM above.
                _set_nested(mapping, f"platform_toolsets.{PLATFORM}", list(body["toolsets"]))
                applied["toolsets"] = list(body["toolsets"])

            warnings.extend(_validated_mapping(mapping))
            atomic_yaml_write(get_config_path(), mapping, sort_keys=False)

        # Keys last: the file is written before a secret is, so a refused
        # config never leaves a key behind it.
        for name, value in (body.get("api_keys") or {}).items():
            if value.strip():
                save_env_value(name, value)
            else:
                remove_env_value(name)
            applied["api_keys"].append(name)

    return applied, warnings
