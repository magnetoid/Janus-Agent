"""Configuration-posture checks for ``janus security audit`` (offline engine).

The existing audit is supply-chain only (OSV.dev lookups). This is the second,
fully offline engine beside it: it reads the merged config plus the raw user
``config.yaml`` and reports *settings* that weaken the operator's safety
story — the class of defaults that burned OpenClaw (host execution for
unattended runs, no spend caps, exposed binds, approvals off).

Design:
  * ``collect()`` is pure and never raises — every check is wrapped; an
    unreadable signal yields no finding rather than a crash.
  * ``--fix`` only ever touches keys in the explicit registry below (each
    finding carries its own ``key``/``safe_value``), backs up ``config.yaml``
    once per real change, writes atomically, and is idempotent.
  * Money (spend caps) is never written unless the operator opts in with
    ``--set-budget-caps``; ``.env`` is never edited.
  * The ``dangerously_*`` convention: existing risky keys are listed in the
    registry (renaming them would be a three-loader change with silent-
    divergence risk); NEW opt-in risky keys must use the prefix, which the
    generic ``config.dangerously_prefixed`` check flags with no code change.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import stat
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

SEVERITIES = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")

_BUDGET_DEFAULTS = {"budget.daily_usd": 25, "budget.monthly_usd": 200}
_BIND_KEYS = {"host", "bind", "bind_host", "listen", "listen_host"}
_EXPOSED = {"0.0.0.0", "::", "[::]"}
# Env vars the adapters read as their listen host (API_SERVER_HOST, LINE_HOST,
# *_WEBHOOK_HOST, …). Client-side hosts (EMAIL_IMAP_HOST) share the suffix but
# never hold an all-interfaces address, so the value test keeps them out.
_ENV_BIND = re.compile(r"^[A-Z][A-Z0-9_]*_HOST$")
# Env hosts that a platform's config.yaml ``extra`` key overrides, mirroring
# each adapter's precedence: name → (platform, extra key, env vars that, when
# all set, make gateway/config.py seed ``extra`` from the env instead).
_ENV_BIND_CONFIG_OVERRIDES: Dict[str, Tuple[str, str, Tuple[str, ...]]] = {
    "FEISHU_WEBHOOK_HOST": ("feishu", "webhook_host", ()),
    "BLUEBUBBLES_WEBHOOK_HOST": ("bluebubbles", "webhook_host",
                                 ("BLUEBUBBLES_SERVER_URL", "BLUEBUBBLES_PASSWORD")),
    "API_SERVER_HOST": ("api_server", "host", ()),  # env seeding: see below
}
# Files under the home that hold secrets or tokens; dirs are scanned one level.
_SECRET_FILES = (".env", "config.yaml", "auth.json", ".anthropic_oauth.json",
                 "webhook_subscriptions.json")
_SECRET_DIRS = ("auth", "mcp-tokens")
_BROAD_CMD = re.compile(r"(^|\s)(rm\s|sudo\b|chmod\s+777|curl[^|]*\|\s*(ba)?sh\b|wget[^|]*\|\s*(ba)?sh\b)")


@dataclass
class PostureFinding:
    check_id: str
    severity: str
    title: str
    detail: str
    key: Optional[str] = None       # dotted config key a fix writes; None → not a config write
    current: Any = None
    safe_value: Any = None
    fixable: bool = False           # True → `--fix` can repair it automatically
    fix_note: str = ""              # how to fix when not auto-fixable (or what --fix does)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["current"] = _jsonable(self.current)
        d["safe_value"] = _jsonable(self.safe_value)
        return d


def _jsonable(v: Any) -> Any:
    if isinstance(v, Path):
        return str(v)
    return v


def _get(d: Any, *path: str, default: Any = None) -> Any:
    cur = d
    for seg in path:
        if not isinstance(cur, Mapping) or seg not in cur:
            return default
        cur = cur[seg]
    return cur


def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in {"1", "true", "yes", "on"}


def _walk_leaves(d: Any, prefix: str = "") -> Iterator[Tuple[str, Any]]:
    if isinstance(d, Mapping):
        for k, v in d.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, Mapping):
                yield from _walk_leaves(v, p)
            else:
                yield p, v


def _unattended_surfaces(raw: Mapping[str, Any], janus_home: Path) -> List[str]:
    """Which no-human-watching surfaces are set up (cron, kanban, gateway)."""
    surfaces: List[str] = []
    try:
        if (janus_home / "cron" / "jobs.json").is_file():
            surfaces.append("cron jobs")
    except Exception:
        pass
    try:
        from janus_cli.kanban_db import kanban_db_path
        if kanban_db_path().is_file():
            surfaces.append("kanban board")
    except Exception:
        pass
    try:
        plats = raw.get("platforms") or _get(raw, "gateway", "platforms") or {}
        if isinstance(plats, Mapping) and plats:
            surfaces.append("messaging gateway")
    except Exception:
        pass
    return surfaces


def _dotenv_values(janus_home: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        p = janus_home / ".env"
        if not p.is_file():
            return out
        for line in p.read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if s.startswith("export "):
                s = s[len("export "):].lstrip()
            if not s or s.startswith("#") or "=" not in s:
                continue
            name, value = s.split("=", 1)
            out.setdefault(name.strip(), value.strip().strip("'\""))
    except Exception:
        pass
    return out


def _dotenv_has_truthy(janus_home: Path, name: str) -> bool:
    return _truthy(_dotenv_values(janus_home).get(name, ""))


def _config_overrides_env_host(name: str, raw: Mapping[str, Any], values: Mapping[str, str]) -> bool:
    """True when config.yaml sets the host the adapter prefers over env var *name*."""
    spec = _ENV_BIND_CONFIG_OVERRIDES.get(name)
    if spec is None:
        return False
    platform, key, env_seed = spec
    if name == "API_SERVER_HOST":
        # gateway/config.py writes API_SERVER_HOST into extra.host whenever the
        # env enables the server (API_SERVER_ENABLED or API_SERVER_KEY).
        if _truthy(values.get("API_SERVER_ENABLED", "")) or values.get("API_SERVER_KEY"):
            return False
    elif env_seed and all(values.get(v) for v in env_seed):
        return False
    plats = raw.get("platforms") or _get(raw, "gateway", "platforms") or {}
    pc = plats.get(platform) if isinstance(plats, Mapping) else None
    if not isinstance(pc, Mapping):
        return False
    extra = pc.get("extra")
    return bool((extra.get(key) if isinstance(extra, Mapping) else None) or pc.get(key))


def _secret_files(janus_home: Path) -> Iterator[Path]:
    for fname in _SECRET_FILES:
        p = janus_home / fname
        if p.is_file():
            yield p
    for dname in _SECRET_DIRS:
        d = janus_home / dname
        if d.is_dir():
            yield from sorted(c for c in d.iterdir() if c.is_file())


# ── checks ───────────────────────────────────────────────────────────────


def collect(
    config: Mapping[str, Any],
    raw: Mapping[str, Any],
    janus_home: Path,
    env: Optional[Mapping[str, str]] = None,
) -> List[PostureFinding]:
    """Evaluate every posture check. Pure; never raises."""
    env = env if env is not None else os.environ
    out: List[PostureFinding] = []

    def check(fn):
        try:
            res = fn()
            if res:
                out.extend(res if isinstance(res, list) else [res])
        except Exception as exc:  # a broken signal is not a finding
            logger.debug("posture check %s skipped: %s", getattr(fn, "__name__", fn), exc)

    def c_guard_agent_created():
        if _get(raw, "skills", "guard_agent_created") is False:
            return PostureFinding(
                "skills.guard_agent_created_off", "HIGH",
                "Agent-created skills are never scanned",
                "skills.guard_agent_created is explicitly false, so skills the agent "
                "writes load unscanned in every context (they are arbitrary Python).",
                key="skills.guard_agent_created", current=False, safe_value="auto", fixable=True,
                fix_note="sets skills.guard_agent_created: auto (scan in headless/cron/kanban)")

    def c_approvals_off():
        mode = _get(config, "approvals", "mode", default="manual")
        if mode is False or str(mode).strip().lower() == "off":
            return PostureFinding(
                "approvals.mode_off", "HIGH", "Tool approvals are disabled",
                "approvals.mode is off: dangerous commands run without any approval gate.",
                key="approvals.mode", current=mode, safe_value="manual", fixable=True,
                fix_note="sets approvals.mode: manual")

    def c_gateway_approver_any():
        v = _get(config, "approvals", "gateway_approver", default="requester")
        if str(v).strip().lower() in {"any", "anyone", "all"}:
            return PostureFinding(
                "approvals.gateway_approver_any", "MEDIUM",
                "Anyone in a chat can approve another user's command",
                "approvals.gateway_approver is 'any': in a group chat, any authorized member "
                "can approve a dangerous command someone else's message triggered.",
                key="approvals.gateway_approver", current=v, safe_value="requester",
                fixable=True, fix_note="sets approvals.gateway_approver: requester")

    def c_unattended_approve():
        res = []
        for k in ("cron_mode", "headless_mode"):
            v = _get(config, "approvals", k, default="deny")
            if str(v).strip().lower() == "approve":
                res.append(PostureFinding(
                    f"approvals.{k}_approve", "HIGH",
                    f"Unattended runs auto-approve dangerous commands ({k})",
                    f"approvals.{k} is 'approve': with nobody watching, every flagged "
                    "command is allowed — a prompt injection in a cron/headless task "
                    "becomes code execution.",
                    key=f"approvals.{k}", current=v, safe_value="deny", fixable=True,
                    fix_note=f"sets approvals.{k}: deny"))
        return res

    def c_yolo():
        if _truthy(env.get("JANUS_YOLO_MODE")) or _dotenv_has_truthy(janus_home, "JANUS_YOLO_MODE"):
            return PostureFinding(
                "env.yolo_mode", "HIGH", "JANUS_YOLO_MODE bypasses all approvals",
                "JANUS_YOLO_MODE is set (environment or .env): every approval check is "
                "skipped process-wide for the lifetime of the session.",
                current=True, safe_value=None, fixable=False,
                fix_note="remove JANUS_YOLO_MODE from your environment and $JANUS_HOME/.env")

    def c_auto_promote_without_governor():
        gov = _get(config, "learning", "governor", default={}) or {}
        if _truthy(gov.get("auto_promote")) and not _truthy(gov.get("enabled")):
            return PostureFinding(
                "learning.auto_promote_without_governor", "HIGH",
                "Skill auto-promotion is on but the health governor is off",
                "learning.governor.auto_promote is true while learning.governor.enabled is "
                "false — drafts can graduate with no collapse/forgetting circuit-breaker.",
                key="learning.governor.auto_promote", current=True, safe_value=False, fixable=True,
                fix_note="sets learning.governor.auto_promote: false (or enable the governor)")

    def c_self_improve_no_human():
        si = _get(config, "learning", "self_improve", default={}) or {}
        if si.get("require_human_approval") is False:
            return PostureFinding(
                "learning.self_improve_no_human", "CRITICAL",
                "Self-modification can promote without a human",
                "learning.self_improve.require_human_approval is false: the agent can "
                "promote changes to its own skills/prompts/policies with no human in the loop.",
                key="learning.self_improve.require_human_approval", current=False,
                safe_value=True, fixable=True,
                fix_note="sets learning.self_improve.require_human_approval: true")

    def c_inline_shell():
        if _truthy(_get(config, "skills", "inline_shell")):
            return PostureFinding(
                "skills.inline_shell", "MEDIUM", "Skills may run inline shell",
                "skills.inline_shell is true: skill files can execute shell snippets at load.",
                current=True, fixable=False,
                fix_note="set skills.inline_shell: false unless a skill you trust needs it")

    def c_sandbox_off_unattended():
        surfaces = _unattended_surfaces(raw, janus_home)
        if not surfaces:
            return None
        backend = str(_get(config, "terminal", "backend", default="local") or "local").lower()
        from tools.environments.headless import resolve_headless_backend
        if backend != "local" or resolve_headless_backend(config):
            return None
        try:
            from tools.environments.docker import docker_usable
            can_fix = bool(docker_usable())
        except Exception:
            can_fix = False
        return PostureFinding(
            "terminal.sandbox_off_unattended", "MEDIUM",
            "Unattended runs execute on the host",
            f"{', '.join(surfaces)} will run commands directly on this machine "
            "(terminal.backend: local, no headless_backend override).",
            key="terminal.headless_backend", current=_get(config, "terminal", "headless_backend"),
            safe_value="docker", fixable=can_fix,
            fix_note=("sets terminal.headless_backend: docker" if can_fix
                      else "install/start Docker, then set terminal.headless_backend: docker"))

    def c_no_caps_unattended():
        surfaces = _unattended_surfaces(raw, janus_home)
        if not surfaces:
            return None
        caps = [float(_get(config, "budget", k, default=0) or 0)
                for k in ("session_cost_usd", "daily_usd", "monthly_usd")]
        if any(c > 0 for c in caps):
            return None
        return PostureFinding(
            "budget.no_caps_unattended", "MEDIUM", "No spend caps for unattended runs",
            f"{', '.join(surfaces)} can spend without limit — budget.session_cost_usd, "
            "daily_usd and monthly_usd are all 0 (runaway-loop cost stories start here).",
            key="budget", current=0, safe_value=dict(_BUDGET_DEFAULTS), fixable=False,
            fix_note="re-run with --fix --set-budget-caps to write daily_usd: 25, monthly_usd: 200, "
                     "or set budget.* yourself")

    def c_frozen():
        from agent.autonomy_guard import freeze_reason_text, frozen
        if frozen(config):
            return PostureFinding(
                "autonomy.frozen", "INFO", "Autonomy is frozen",
                f"New cron/kanban/delegation/self-improvement work is blocked: {freeze_reason_text()}",
                current=True, fixable=False, fix_note="janus autonomy unfreeze (when intended)")

    def c_exposed_bind():
        res = []
        for dotted, v in _walk_leaves(raw):
            if dotted.rsplit(".", 1)[-1] in _BIND_KEYS and str(v).strip() in _EXPOSED:
                res.append(PostureFinding(
                    "network.exposed_bind", "HIGH", f"{dotted} binds to all interfaces",
                    f"{dotted}: {v} exposes a Janus service beyond localhost; tens of thousands "
                    "of OpenClaw instances were found this way.",
                    key=dotted, current=v, safe_value="127.0.0.1", fixable=True,
                    fix_note=f"sets {dotted}: 127.0.0.1 (use a VPN/SSH tunnel for remote access)"))
        # Adapters fall back to env vars when config is silent (API_SERVER_HOST…).
        # The process env wins over .env, as it does when .env is loaded.
        dotenv = _dotenv_values(janus_home)
        sources = {**{k: ".env" for k in dotenv}, **{k: "environment" for k in env}}
        values = {**dotenv, **dict(env)}
        for name in sorted(sources):
            if (_ENV_BIND.match(name) and str(values.get(name, "")).strip() in _EXPOSED
                    and not _config_overrides_env_host(name, raw, values)):
                res.append(PostureFinding(
                    "network.exposed_bind", "HIGH", f"{name} binds to all interfaces",
                    f"{name}={values[name]} (from {sources[name]}) exposes a Janus service "
                    "beyond localhost.",
                    current=values[name], safe_value="127.0.0.1", fixable=False,
                    fix_note=f"set {name}=127.0.0.1 in {sources[name]} "
                             "(use a VPN/SSH tunnel for remote access)"))
        return res

    def c_allowlist_empty():
        res = []
        plats = raw.get("platforms") or _get(raw, "gateway", "platforms") or {}
        if not isinstance(plats, Mapping):
            return res
        for name, pc in plats.items():
            if not isinstance(pc, Mapping):
                continue
            allow = pc.get("allow_from")
            behavior = str(pc.get("unauthorized_dm_behavior")
                           or _get(raw, "gateway", "unauthorized_dm_behavior")
                           or raw.get("unauthorized_dm_behavior") or "pair").lower()
            if not allow and behavior != "pair":
                res.append(PostureFinding(
                    "gateway.allowlist_empty", "MEDIUM",
                    f"{name}: no allow_from and unknown DMs are not paired",
                    f"platforms.{name} has no allow_from list and unauthorized_dm_behavior is "
                    f"'{behavior}' — anyone who finds the bot can message it.",
                    current=behavior, fixable=False,
                    fix_note="set allow_from, or unauthorized_dm_behavior: pair (janus pairing)"))
        return res

    def c_file_perms():
        res = []
        if os.name != "posix":
            return res
        for p in _secret_files(janus_home):
            mode = stat.S_IMODE(p.stat().st_mode)
            if mode & 0o077:
                rel = p.relative_to(janus_home).as_posix()
                res.append(PostureFinding(
                    "files.permissions", "LOW", f"{rel} is readable by other users",
                    f"{p} has mode {oct(mode)}; it holds secrets/tokens and should be 0600.",
                    current=p, safe_value="0600", fixable=True, fix_note=f"chmod 600 {p}"))
        return res

    def c_broad_allowlist():
        res = []
        for entry in config.get("command_allowlist") or []:
            s = str(entry)
            if _BROAD_CMD.search(s):
                res.append(PostureFinding(
                    "command_allowlist.broad", "MEDIUM", "Broad command_allowlist entry",
                    f"'{s}' is pre-approved; it matches destructive/privileged patterns.",
                    current=s, fixable=False, fix_note="narrow or remove the entry"))
        return res

    def c_dangerously_prefixed():
        res = []
        for dotted, v in _walk_leaves(raw):
            if dotted.rsplit(".", 1)[-1].startswith("dangerously_") and _truthy(v):
                res.append(PostureFinding(
                    "config.dangerously_prefixed", "HIGH", f"{dotted} is enabled",
                    f"{dotted} is a deliberately risky opt-in (the prefix marks it).",
                    key=dotted, current=v, fixable=False, fix_note=f"set {dotted}: false"))
        return res

    for fn in (c_guard_agent_created, c_approvals_off, c_gateway_approver_any, c_unattended_approve, c_yolo,
               c_auto_promote_without_governor, c_self_improve_no_human, c_inline_shell,
               c_sandbox_off_unattended, c_no_caps_unattended, c_frozen, c_exposed_bind,
               c_allowlist_empty, c_file_perms, c_broad_allowlist, c_dangerously_prefixed):
        check(fn)
    return out


# ── fixes ────────────────────────────────────────────────────────────────


def apply_fixes(
    findings: List[PostureFinding],
    *,
    config_path: Path,
    dry_run: bool = False,
    set_budget_caps: bool = False,
) -> List[PostureFinding]:
    """Repair auto-fixable findings. Returns the findings that were (or would be) applied.

    Config writes go through one backed-up, atomic YAML write; only each
    finding's own ``key``/``safe_value`` is touched. Idempotent: re-running on
    a fixed config changes nothing."""
    config_writes = [f for f in findings if f.fixable and f.key and f.check_id != "files.permissions"]
    budget = [f for f in findings if f.check_id == "budget.no_caps_unattended"] if set_budget_caps else []
    perms = [f for f in findings if f.check_id == "files.permissions" and f.fixable]
    planned = config_writes + budget + perms
    if not planned or dry_run:
        return planned

    applied: List[PostureFinding] = []
    if config_writes or budget:
        import yaml
        from janus_cli.config import _set_nested, ensure_janus_home
        from utils import atomic_yaml_write

        raw: Dict[str, Any] = {}
        if config_path.exists():
            try:
                raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
            except Exception:
                raw = {}
            backup = config_path.with_suffix(
                f".yaml.bak.{datetime.now().strftime('%Y%m%d_%H%M%S')}")
            try:
                shutil.copy2(config_path, backup)
            except Exception as exc:
                logger.debug("posture backup failed: %s", exc)
        for f in config_writes:
            _set_nested(raw, f.key, f.safe_value)
            applied.append(f)
        for f in budget:
            for k, v in _BUDGET_DEFAULTS.items():
                _set_nested(raw, k, v)
            applied.append(f)
        ensure_janus_home()
        atomic_yaml_write(config_path, raw, sort_keys=False)
    for f in perms:
        try:
            os.chmod(Path(f.current), 0o600)
            applied.append(f)
        except Exception as exc:
            logger.debug("chmod failed for %s: %s", f.current, exc)
    return applied


# ── rendering ────────────────────────────────────────────────────────────

_ICON = {"CRITICAL": "✕", "HIGH": "✕", "MEDIUM": "⚠", "LOW": "·", "INFO": "ℹ"}


def render_human(findings: List[PostureFinding], applied: Optional[List[PostureFinding]] = None,
                 dry_run: bool = False) -> str:
    lines = ["", "Configuration posture"]
    if not findings:
        lines.append("  ✓ no posture issues found")
        return "\n".join(lines)
    applied_ids = {id(f) for f in (applied or [])}
    order = {s: i for i, s in enumerate(reversed(SEVERITIES))}
    for f in sorted(findings, key=lambda x: order.get(x.severity, 99)):
        tag = ("  [would fix]" if dry_run and id(f) in applied_ids else
               "  [fixed]" if id(f) in applied_ids else
               "  [fixable: --fix]" if f.fixable else "")
        lines.append(f"  {_ICON.get(f.severity, '·')} {f.severity:<8} {f.title}{tag}")
        lines.append(f"      {f.detail}")
        if f.fix_note:
            lines.append(f"      → {f.fix_note}")
    return "\n".join(lines)
