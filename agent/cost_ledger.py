"""Per-turn cost ledger + per-session spend ceiling.

Appends one JSONL row per API turn (session, model, tokens, cost) under
``$JANUS_HOME/learning/cost_ledger.jsonl`` so spend is auditable after the fact,
and exposes the configured per-session USD ceiling the conversation loop checks
at the turn boundary to halt a runaway session.

Pure, best-effort, never raises. The ceiling only *stops the loop earlier* — it
never mutates the live conversation / system prompt / toolset, so the
prompt-caching invariant holds. See plans/self-improvement-roadmap (Track B / B-PR2)
and docs/token-optimization-audit.md.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def ledger_path() -> Path:
    from janus_constants import get_janus_home
    return get_janus_home() / "learning" / "cost_ledger.jsonl"


def _now_iso() -> str:
    try:
        from janus_time import now as _now
        return _now().isoformat()
    except Exception:
        return ""


# Conservative per-1k-token fallback for models the pricing layer can't price
# (gap G9). A real $0 (no usage) stays $0; a $0 turn that DID burn tokens gets
# this estimate so it is not invisible to the rolling spend cap. Errs toward
# over-counting — the safe direction for a SAFETY floor. Override with
# ``budget.unpriced_usd_per_1k_tokens``.
_DEFAULT_UNPRICED_USD_PER_1K = 0.01


def _unpriced_rate(config: Optional[Dict[str, Any]] = None) -> float:
    try:
        if config is None:
            from janus_cli.config import load_config
            config = load_config()
        budget = (config.get("budget", {}) or {}) if isinstance(config, dict) else {}
        v = budget.get("unpriced_usd_per_1k_tokens")
        return float(v) if v is not None and float(v) >= 0 else _DEFAULT_UNPRICED_USD_PER_1K
    except Exception:
        return _DEFAULT_UNPRICED_USD_PER_1K


def record_turn(
    session_id: str, model: str, *,
    input_tokens: int = 0, output_tokens: int = 0, cache_read_tokens: int = 0,
    cost_usd: float = 0.0, status: str = "", ts: str = "",
    config: Optional[Dict[str, Any]] = None,
) -> bool:
    """Append one turn's usage + cost to the ledger. Best-effort; returns True
    if a row was written. Never raises.

    When ``cost_usd`` is 0 but the turn used tokens on a model the pricing layer
    genuinely COULD NOT price, a conservative estimate is recorded and the row is
    flagged ``estimated`` so the spend cap isn't blind to it (gap G9). A real $0 —
    a subscription-covered (``status="included"``) or measured-zero
    (``status="actual"``) turn — is left at $0: those are genuinely free, and
    estimating them would charge phantom cost that could trip a subscription
    user's caps."""
    try:
        in_tok = int(input_tokens or 0)
        out_tok = int(output_tokens or 0)
        cost = round(float(cost_usd or 0.0), 6)
        estimated = False
        # Only a genuinely-unpriced turn is estimated. "included"/"actual" (and
        # "estimated", already valued) are real $0 and stay $0; an empty status
        # is treated as unknown (the default for direct callers).
        _priced_zero = str(status or "").lower() in ("actual", "included", "estimated")
        if cost <= 0.0 and (in_tok + out_tok) > 0 and not _priced_zero:
            cost = round((in_tok + out_tok) / 1000.0 * _unpriced_rate(config), 6)
            estimated = cost > 0.0
        row = {
            "ts": ts or _now_iso(),
            "session_id": session_id or "",
            "model": model or "",
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_read_tokens": int(cache_read_tokens or 0),
            "cost_usd": cost,
            "estimated": estimated,
            "status": status or "",
        }
        path = ledger_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        return True
    except Exception as exc:
        logger.debug("cost ledger write failed: %s", exc)
        return False


def session_cost_limit_usd(config: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """The per-session USD ceiling from ``budget.session_cost_usd``. Returns None
    when unset / <= 0 (unlimited)."""
    try:
        if config is None:
            from janus_cli.config import load_config
            config = load_config()
        budget = (config.get("budget", {}) or {}) if isinstance(config, dict) else {}
        v = budget.get("session_cost_usd", 0)
        v = float(v) if v is not None else 0.0
        return v if v > 0 else None
    except Exception:
        return None


def over_session_budget(spent_usd: float, limit_usd: Optional[float]) -> bool:
    """True when an enforced ceiling has been reached. Pure; used by the loop gate."""
    try:
        return limit_usd is not None and limit_usd > 0 and float(spent_usd or 0.0) >= limit_usd
    except Exception:
        return False


def load_ledger() -> List[Dict[str, Any]]:
    """Read all ledger rows (for /insights or tests). Best-effort → []."""
    try:
        path = ledger_path()
        if not path.is_file():
            return []
        rows: List[Dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
        return rows
    except Exception:
        return []


def session_total_usd(session_id: str) -> float:
    """Sum of ``cost_usd`` for one session from the ledger. Best-effort → 0.0."""
    try:
        return round(
            sum(float(r.get("cost_usd", 0.0)) for r in load_ledger()
                if r.get("session_id") == session_id),
            6,
        )
    except Exception:
        return 0.0


# --- rolling spend windows (move 3: safety floor) ---------------------------

_PERIOD_SECONDS = {"day": 86400.0, "month": 2592000.0}  # trailing 24h / 30d


def _parse_ts(ts: Any) -> Optional[float]:
    """ISO-8601 string → epoch seconds. None on anything unparseable."""
    if not ts:
        return None
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(ts)).timestamp()
    except Exception:
        return None


def spend_since(cutoff_epoch: float, *, rows: Optional[List[Dict[str, Any]]] = None) -> float:
    """Sum ``cost_usd`` across ALL sessions for rows at/after ``cutoff_epoch``.

    Rows with an unparseable/missing ts are EXCLUDED. Counting them would be
    monotonic and unbounded — a handful of empty-ts rows (``_now_iso`` returns
    "" when janus_time hiccups) would pin every rolling window over the cap
    forever, permanently blocking autonomy with no recovery. Ledger-write access
    already defeats any ledger-based cap (you could delete the file), so this
    trades a non-defense against a real self-inflicted lockout.
    """
    try:
        rows = load_ledger() if rows is None else rows
        total = 0.0
        for r in rows:
            rt = _parse_ts(r.get("ts"))
            if rt is not None and rt >= cutoff_epoch:
                total += float(r.get("cost_usd", 0.0) or 0.0)
        return round(total, 6)
    except Exception:
        return 0.0


def rolling_spend(period: str, *, now_ts: Optional[float] = None) -> float:
    """Total spend over the trailing ``period`` ("day" = 24h, "month" = 30d)."""
    window = _PERIOD_SECONDS.get(period)
    if window is None:
        return 0.0
    if now_ts is None:
        import time
        now_ts = time.time()
    return spend_since(now_ts - window)


def rolling_caps(config: Optional[Dict[str, Any]] = None) -> Dict[str, float]:
    """Configured rolling USD caps ``{"day": x, "month": y}`` (0 = unlimited)."""
    try:
        if config is None:
            from janus_cli.config import load_config
            config = load_config()
        budget = (config.get("budget", {}) or {}) if isinstance(config, dict) else {}
        out: Dict[str, float] = {}
        for period, key in (("day", "daily_usd"), ("month", "monthly_usd")):
            try:
                v = float(budget.get(key, 0) or 0)
            except (TypeError, ValueError):
                v = 0.0
            if v > 0:
                out[period] = v
        return out
    except Exception:
        return {}


def spend_cap_exceeded(config: Optional[Dict[str, Any]] = None, *,
                       now_ts: Optional[float] = None) -> Optional[str]:
    """Reason string if any rolling cap is met/exceeded, else None. Best-effort.

    This spans ALL sessions (unlike the per-session ceiling), so it is the cap
    that bounds unattended fan-out — many cheap cron/kanban/delegate sessions
    that individually stay under the per-session ceiling but together run away.
    """
    try:
        caps = rolling_caps(config)
        if not caps:
            return None
        for period, cap in caps.items():
            spent = rolling_spend(period, now_ts=now_ts)
            if spent >= cap:
                label = "24h" if period == "day" else "30d"
                return (f"rolling {label} spend ${spent:.2f} has reached the "
                        f"${cap:.2f} cap (budget.{'daily_usd' if period == 'day' else 'monthly_usd'})")
        return None
    except Exception:
        return None


def cache_efficiency(last_n: int = 200) -> Dict[str, Any]:
    """Prompt-cache efficiency over the last ``last_n`` ledger turns.

    ``hit_rate`` = cache_read / (cache_read + fresh input) — the fraction of
    prompt context served from the provider cache instead of re-billed at the
    fresh-token rate. The #1 per-turn cost lever: cache discipline beats
    context minimalism (a cache-cold agent pays several times more per turn
    than a cache-warm one with a LARGER context). None when no tokens
    recorded. Also reports how many distinct models the window saw per
    session — mid-session model switching silently invalidates the entire
    provider cache, so >1 is a cost smell worth surfacing.
    """
    rows = load_ledger()[-max(1, int(last_n)):]
    read = sum(int(r.get("cache_read_tokens", 0) or 0) for r in rows)
    fresh = sum(int(r.get("input_tokens", 0) or 0) for r in rows)
    total = read + fresh
    models_per_session: Dict[str, set] = {}
    for r in rows:
        sid = str(r.get("session_id", "") or "")
        if sid and r.get("model"):
            models_per_session.setdefault(sid, set()).add(str(r["model"]))
    switched = sum(1 for models in models_per_session.values() if len(models) > 1)
    return {
        "turns": len(rows),
        "cache_read_tokens": read,
        "fresh_input_tokens": fresh,
        "hit_rate": (read / total) if total else None,
        "sessions_with_model_switch": switched,
    }
