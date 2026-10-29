"""``janus delegation profiles`` — named subagent profiles (plan item 3.2).

Profiles live under ``delegation.profiles.<name>`` in ``config.yaml`` and are
selected with ``delegate_task(profile="<name>")``. Each may set
``provider`` / ``model`` / ``base_url`` / ``api_key`` / ``api_mode``,
``toolsets``, ``max_iterations``, ``budget_usd`` and ``role``.
"""
from __future__ import annotations

import json
import re
import sys
from typing import Any, Dict, List, Optional

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_ROLES = ("leaf", "orchestrator")


def _ask(prompt: str, default: str = "") -> str:
    """One prompt of the guided picker; blank keeps ``default``."""
    suffix = f" [{default}]" if default else ""
    try:
        answer = input(f"  {prompt}{suffix}: ").strip()
    except EOFError:
        answer = ""
    return answer or default


def _load() -> Dict[str, Any]:
    from janus_cli.config import load_config
    return load_config()


def _profiles(cfg: Dict[str, Any]) -> Dict[str, Any]:
    deleg = cfg.get("delegation")
    if not isinstance(deleg, dict):
        return {}
    profiles = deleg.get("profiles")
    return profiles if isinstance(profiles, dict) else {}


def _save_profiles(cfg: Dict[str, Any], profiles: Dict[str, Any]) -> None:
    from janus_cli.config import save_config
    deleg = cfg.get("delegation")
    if not isinstance(deleg, dict):
        deleg = {}
        cfg["delegation"] = deleg
    deleg["profiles"] = profiles
    save_config(cfg)


def _toolsets(raw: Any) -> Optional[List[str]]:
    if raw is None:
        return None
    if isinstance(raw, (list, tuple)):
        items = [str(t).strip() for t in raw]
    else:
        items = [t.strip() for t in str(raw).split(",")]
    items = [t for t in items if t]
    return items or None


def _build_profile(values: Dict[str, Any]) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Validate raw answers/flags into a profile dict; blank values are omitted."""
    prof: Dict[str, Any] = {}
    for key in ("provider", "model", "base_url", "api_mode"):
        value = str(values.get(key) or "").strip()
        if value:
            prof[key] = value
    toolsets = _toolsets(values.get("toolsets"))
    if toolsets:
        prof["toolsets"] = toolsets
    raw_iter = values.get("max_iterations")
    if raw_iter not in (None, ""):
        try:
            iters = int(raw_iter)
        except (TypeError, ValueError):
            return None, f"max_iterations must be a whole number, got {raw_iter!r}"
        if iters < 1:
            return None, "max_iterations must be at least 1"
        prof["max_iterations"] = iters
    raw_budget = values.get("budget_usd")
    if raw_budget not in (None, ""):
        try:
            budget = float(raw_budget)
        except (TypeError, ValueError):
            return None, f"budget_usd must be a number, got {raw_budget!r}"
        if budget <= 0:
            return None, "budget_usd must be greater than 0 (leave it out for no cap)"
        prof["budget_usd"] = budget
    role = str(values.get("role") or "").strip().lower()
    if role:
        if role not in _ROLES:
            return None, f"role must be one of {', '.join(_ROLES)}, got {role!r}"
        prof["role"] = role
    return prof, None


def _cmd_list(args) -> int:
    profiles = _profiles(_load())
    if getattr(args, "json", False):
        print(json.dumps(profiles, indent=2, ensure_ascii=False, default=str))
        return 0
    if not profiles:
        print("\n  No delegation profiles configured.\n"
              "  Add one with `janus delegation profiles add`.\n")
        return 0
    print()
    for name in sorted(profiles):
        prof = profiles[name] if isinstance(profiles[name], dict) else {}
        target = "/".join(x for x in (prof.get("provider"), prof.get("model")) if x) \
            or prof.get("base_url") or "inherit parent model"
        print(f"  {name}")
        print(f"      model:    {target}")
        if prof.get("toolsets"):
            print(f"      toolsets: {', '.join(prof['toolsets'])}")
        caps = []
        if prof.get("max_iterations"):
            caps.append(f"{prof['max_iterations']} iterations")
        if prof.get("budget_usd"):
            caps.append(f"${float(prof['budget_usd']):.2f}")
        if caps:
            print(f"      caps:     {', '.join(caps)}")
        if prof.get("role"):
            print(f"      role:     {prof['role']}")
    print("\n  use: delegate_task(goal=..., profile=\"<name>\")\n")
    return 0


def _guided(args) -> Dict[str, Any]:
    print("\n  New delegation profile (blank = leave unset)\n")
    return {
        "name": getattr(args, "name", None) or _ask("Name"),
        "model": getattr(args, "model", None) or _ask("Model (e.g. google/gemini-3-flash-preview)"),
        "provider": getattr(args, "provider", None) or _ask("Provider (e.g. openrouter)"),
        "toolsets": getattr(args, "toolsets", None) or _ask("Toolsets, comma separated (e.g. web,file)"),
        "max_iterations": getattr(args, "max_iterations", None) or _ask("Max iterations"),
        "budget_usd": getattr(args, "budget_usd", None) or _ask("Budget per child in USD"),
        "role": getattr(args, "role", None) or _ask("Role (leaf/orchestrator)"),
    }


def _cmd_add(args) -> int:
    interactive = bool(getattr(args, "interactive", False)) or (
        not getattr(args, "name", None) and sys.stdin.isatty()
    )
    if interactive:
        values = _guided(args)
    else:
        values = {k: getattr(args, k, None) for k in (
            "name", "model", "provider", "base_url", "api_mode", "toolsets",
            "max_iterations", "budget_usd", "role")}
    name = str(values.get("name") or "").strip()
    if not name or not _NAME_RE.match(name):
        print(f"  ✗ Profile name must be letters, digits, '-', '_' or '.', got {name!r}")
        return 1
    prof, error = _build_profile(values)
    if error or prof is None:
        print(f"  ✗ {error}")
        return 1
    cfg = _load()
    profiles = dict(_profiles(cfg))
    if name in profiles and not getattr(args, "force", False):
        print(f"  ✗ Profile '{name}' already exists (use --force to replace it).")
        return 1
    profiles[name] = prof
    _save_profiles(cfg, profiles)
    print(f"  ✓ Saved delegation profile '{name}'. Use it with "
          f"delegate_task(goal=..., profile=\"{name}\").")
    return 0


def _cmd_remove(args) -> int:
    name = str(getattr(args, "name", "") or "").strip()
    cfg = _load()
    profiles = dict(_profiles(cfg))
    if name not in profiles:
        print(f"  ✗ No delegation profile named '{name}'.")
        return 1
    del profiles[name]
    _save_profiles(cfg, profiles)
    print(f"  ✓ Removed delegation profile '{name}'.")
    return 0


def delegation_command(args) -> int:
    if getattr(args, "delegation_command", None) != "profiles":
        print("  usage: janus delegation profiles [list|add|remove]")
        return 1
    sub = getattr(args, "profiles_command", None) or "list"
    if sub == "add":
        return _cmd_add(args)
    if sub == "remove":
        return _cmd_remove(args)
    return _cmd_list(args)
