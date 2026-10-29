"""Read-only health check for built-in memory (plan item 3.4).

Findings (each ``{kind, target, entry, detail}``):

* ``contradiction`` — superseded or contradicted by another entry, from the
  same check ``janus memory reconcile`` uses (one auxiliary model call per
  target; skipped with ``offline=True``).
* ``duplicate`` — same text up to case and whitespace.
* ``unknown_origin`` / ``untrusted_origin`` / ``stale`` — from the provenance
  sidecar, only when ``memory.provenance`` is on: no recorded origin, written
  while untrusted external content was in context, or older than
  ``stale_days``.

Nothing is changed; ``janus memory reconcile`` is the fixing tool.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Dict, List, Optional

from agent import memory_provenance as prov

_DAY = 86400.0


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def lint(store: Any, *, targets: tuple = ("memory", "user"), offline: bool = False,
         llm_caller: Optional[Callable[..., Any]] = None, stale_days: int = 180,
         now: Optional[float] = None) -> List[Dict[str, Any]]:
    now = time.time() if now is None else now
    findings: List[Dict[str, Any]] = []
    use_prov = prov.enabled()
    for target in targets:
        entries = list(getattr(store, f"{target}_entries", []) or [])
        seen: Dict[str, str] = {}
        for entry in entries:
            key = _norm(entry)
            if key in seen:
                findings.append({"kind": "duplicate", "target": target, "entry": entry,
                                 "detail": f"same as: {seen[key]}"})
            else:
                seen[key] = entry
        if use_prov:
            for entry in entries:
                rec = prov.lookup(target, entry)
                if rec is None:
                    findings.append({"kind": "unknown_origin", "target": target, "entry": entry,
                                     "detail": "no provenance recorded (written before provenance "
                                               "was on, or edited outside Janus)"})
                    continue
                if rec.get("untrusted"):
                    findings.append({"kind": "untrusted_origin", "target": target, "entry": entry,
                                     "detail": f"written by {rec.get('source')} while untrusted "
                                               f"external content was in context"})
                age = now - float(rec.get("created_at") or now)
                if stale_days and age > stale_days * _DAY:
                    findings.append({"kind": "stale", "target": target, "entry": entry,
                                     "detail": f"{int(age // _DAY)} days old"})
        if not offline and len(entries) >= 2:
            try:
                from agent.memory_gardener import find_stale
                for item in find_stale(entries, llm_caller=llm_caller):
                    kept = item.get("kept")
                    kept_text = entries[kept] if isinstance(kept, int) and 0 <= kept < len(entries) else ""
                    reason = item.get("reason") or "superseded"
                    detail = f"{reason}" + (f" (see: {kept_text})" if kept_text else "")
                    findings.append({"kind": "contradiction", "target": target,
                                     "entry": entries[item["drop"]], "detail": detail})
            except Exception as exc:
                findings.append({"kind": "check_failed", "target": target, "entry": "",
                                 "detail": f"contradiction check failed: {exc}"})
    return findings
