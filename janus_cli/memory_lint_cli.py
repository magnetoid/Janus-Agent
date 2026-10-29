"""``janus memory lint`` — report contradictions, duplicates and doubtful origins."""
from __future__ import annotations

import json
from typing import Any

_LABEL = {
    "contradiction": "contradicted / superseded",
    "duplicate": "duplicate",
    "unknown_origin": "unknown origin",
    "untrusted_origin": "untrusted origin",
    "stale": "stale",
    "check_failed": "check failed",
}


def _load_store() -> Any:
    from tools.memory_tool import MemoryStore
    store = MemoryStore()
    store.load_from_disk()
    return store


def run(args) -> int:
    from agent.memory_lint import lint
    target = getattr(args, "target", "all") or "all"
    targets = ("memory", "user") if target == "all" else (target,)
    findings = lint(_load_store(), targets=targets, offline=bool(getattr(args, "offline", False)),
                    stale_days=int(getattr(args, "stale_days", 180) or 0))
    if getattr(args, "json", False):
        print(json.dumps(findings, indent=2, ensure_ascii=False))
    elif not findings:
        print("\n  Memory looks consistent — nothing to report.\n")
    else:
        print(f"\n  {len(findings)} finding(s):\n")
        for f in findings:
            entry = (f.get("entry") or "").replace("\n", " ")
            if len(entry) > 100:
                entry = entry[:97] + "..."
            print(f"  [{f['target']}] {_LABEL.get(f['kind'], f['kind'])}: {entry}")
            if f.get("detail"):
                print(f"      {f['detail']}")
        print("\n  Fix contradictions with `janus memory reconcile`; edit or remove the rest "
              "with the memory tool.\n")
    return 1 if (findings and getattr(args, "strict", False)) else 0
