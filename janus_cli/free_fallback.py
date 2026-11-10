"""Curated free-tier fallback presets (``janus fallback add --free``).

A short, hand-checked list of official free tiers that can carry an agent
turn (tool calling, usable rate limits) when the paid providers run out.
Each preset states, from the provider's own free-tier terms, whether that
tier trains on what you send it. Checked 2026-11; terms and model names
change, so the add flow lets you override the model and the docs say so.

Deliberately not imported from community lists wholesale: third-party
brokers, anonymous "stealth" models and reverse-engineered endpoints would
see the whole conversation (memory, files, tool output) when a fallback
fires, usually while nobody is watching.

Guard: a preset entry whose tier may train is skipped at fallback time
unless the user said yes when adding it (``allow_training: true`` on the
entry) or turned on ``fallback.allow_training_providers``. Entries added
through the normal picker are never touched by this guard.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

TRAINS_VALUES = ("no", "yes", "depends")


@dataclass(frozen=True)
class FreePreset:
    key: str
    label: str
    provider: str          # a Janus provider slug, or "custom" with base_url
    model: str
    key_env: str
    signup_url: str
    limits: str
    trains: str            # "no" | "yes" | "depends" — from the free tier's terms
    data_note: str
    base_url: str = ""


FREE_FALLBACK_PRESETS: tuple[FreePreset, ...] = (
    FreePreset(
        key="groq",
        label="Groq",
        provider="custom",
        base_url="https://api.groq.com/openai/v1",
        model="moonshotai/kimi-k2-instruct",
        key_env="GROQ_API_KEY",
        signup_url="https://console.groq.com/keys",
        limits="about 30 requests/min with a daily cap per model",
        trains="no",
        data_note="Groq states it does not train on API inputs",
    ),
    FreePreset(
        key="openrouter-free",
        label="OpenRouter (:free models)",
        provider="openrouter",
        model="nvidia/nemotron-3-ultra-550b-a55b:free",
        key_env="OPENROUTER_API_KEY",
        signup_url="https://openrouter.ai/keys",
        limits="50 free requests/day (1,000/day after a one-time $10 credit purchase)",
        trains="depends",
        data_note=("free models are often served by providers that may train on inputs; "
                   "OpenRouter's privacy settings can exclude them"),
    ),
    FreePreset(
        key="gemini-free",
        label="Google Gemini (free quota)",
        provider="gemini",
        model="gemini-flash-latest",
        key_env="GEMINI_API_KEY",
        signup_url="https://aistudio.google.com/apikey",
        limits="per-model per-minute and daily caps on the free quota",
        trains="yes",
        data_note=("Google uses free-quota prompts and responses to improve its products, and "
                   "human reviewers may read them (paid usage is excluded)"),
    ),
    FreePreset(
        key="mistral-experiment",
        label="Mistral (Experiment plan)",
        provider="custom",
        base_url="https://api.mistral.ai/v1",
        model="mistral-small-latest",
        key_env="MISTRAL_API_KEY",
        signup_url="https://console.mistral.ai/api-keys",
        limits="about 1 request/second on the Experiment plan",
        trains="yes",
        data_note=("Experiment-plan inputs and outputs may be used for training unless you "
                   "opt out in Mistral's admin console"),
    ),
    FreePreset(
        key="nvidia-trial",
        label="NVIDIA NIM (API trial)",
        provider="nvidia",
        model="z-ai/glm-5.2",
        key_env="NVIDIA_API_KEY",
        signup_url="https://build.nvidia.com/",
        limits="about 40 requests/min; trial use only, not for production",
        trains="yes",
        data_note="NVIDIA's API trial terms allow it to use prompts and outputs to improve its products",
    ),
)

_BY_KEY = {p.key: p for p in FREE_FALLBACK_PRESETS}


def get_preset(key: Any) -> Optional[FreePreset]:
    return _BY_KEY.get(str(key or ""))


def preset_entry(preset: FreePreset, *, model: str = "", allow_training: bool = False) -> Dict[str, Any]:
    """The ``fallback_providers`` entry for a preset."""
    entry: Dict[str, Any] = {
        "provider": preset.provider,
        "model": model or preset.model,
        "free_preset": preset.key,
    }
    if preset.base_url:
        entry["base_url"] = preset.base_url
        entry["key_env"] = preset.key_env
    if allow_training:
        entry["allow_training"] = True
    return entry


def _trains(entry: Dict[str, Any]) -> str:
    # Always from the table, never from a value copied into config, so a
    # later correction to a preset applies to entries saved before it.
    preset = get_preset(entry.get("free_preset"))
    return preset.trains if preset else "depends"


def blocked_by_training_guard(entry: Dict[str, Any], *, allow_training_providers: bool) -> bool:
    """True when a free-preset entry may train and nobody agreed to that."""
    if not entry.get("free_preset"):
        return False
    if _trains(entry) == "no":
        return False
    return not (allow_training_providers or entry.get("allow_training") is True)


def allow_training_providers_enabled() -> bool:
    """``fallback.allow_training_providers`` (default false). Never raises."""
    try:
        from janus_cli.config import load_config
        section = load_config().get("fallback") or {}
        return isinstance(section, dict) and section.get("allow_training_providers") is True
    except Exception:
        return False


def switch_warning(entry: Dict[str, Any]) -> Optional[str]:
    """User-visible line for a switch to a free-preset fallback, else None."""
    if not entry.get("free_preset"):
        return None
    preset = get_preset(entry.get("free_preset"))
    label = preset.label if preset else str(entry.get("provider") or "?")
    note = preset.data_note if preset else "this free tier's data use is not classified"
    return (f"⚠ Switched to a free fallback: {entry.get('model')} via {label}. "
            f"Data use: {note}.")


# ---------------------------------------------------------------------------
# `janus fallback add --free`
# ---------------------------------------------------------------------------

def _trains_label(preset: FreePreset) -> str:
    return {"no": "does not train on inputs", "yes": "may train on inputs",
            "depends": "may train, depends on the model's host"}[preset.trains]


def cmd_add_free() -> None:
    import getpass

    from janus_cli.config import get_env_value, load_config, save_config, save_env_value
    from janus_cli.fallback_cmd import _format_entry, _numbered_pick, _read_chain, _write_chain

    print()
    print("  Free-tier fallbacks. Used only when every provider before them fails.")
    print("  Free tiers have tight limits, and several of them train on what you send.")
    print()
    choices = [f"{p.label:<28} {p.limits}\n       data: {_trains_label(p)}"
               for p in FREE_FALLBACK_PRESETS]
    idx = _numbered_pick("  Pick a free tier:", choices)
    if idx is None:
        print("\n  No fallback added.")
        return
    preset = FREE_FALLBACK_PRESETS[idx]

    model = input(f"  Model [{preset.model}]: ").strip() or preset.model

    if not (get_env_value(preset.key_env) or "").strip():
        print(f"\n  Get a free key at {preset.signup_url}")
        key = getpass.getpass(f"  {preset.label} API key ({preset.key_env}): ").strip()
        if not key:
            print("\n  No key entered. No fallback added.")
            return
        save_env_value(preset.key_env, key)

    allow_training = False
    if preset.trains != "no":
        print()
        print(f"  ⚠ {preset.label}: {preset.data_note}.")
        print("    If this fallback fires, the whole conversation is sent there, including")
        print("    memory, file contents and tool output.")
        answer = input("  Allow this free tier anyway? [y/N]: ").strip().lower()
        if answer not in {"y", "yes"}:
            print("\n  No fallback added.")
            return
        allow_training = True

    entry = preset_entry(preset, model=model, allow_training=allow_training)
    config = load_config()
    chain = _read_chain(config)
    for existing in chain:
        if existing.get("provider") == entry["provider"] and existing.get("model") == entry["model"] \
                and (existing.get("base_url") or "") == (entry.get("base_url") or ""):
            print(f"\n  {_format_entry(entry)} is already in the fallback chain — skipped.")
            return
    chain.append(entry)
    _write_chain(config, chain)
    save_config(config)

    print()
    print(f"  Added free fallback: {_format_entry(entry)}")
    print(f"  Chain is now {len(chain)} {'entry' if len(chain) == 1 else 'entries'} long.")
    print("  Janus tells you in the chat whenever it switches to a free fallback.")
