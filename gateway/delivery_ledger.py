"""Delivery-obligation ledger — replies the gateway owes a platform.

The gateway's most visible reliability failure is a reply that the agent
produced but the user never saw: the process restarted between "turn
finished" and "message acked", or the platform API stalled past the
in-process retries in ``BasePlatformAdapter._send_with_retry``. This module
makes that reply durable.

Flow:

1. ``record()`` writes a row to ``state.db`` (``delivery_obligations``,
   status ``sending``) *before* the live send.
2. ``settle()`` marks it ``delivered`` on ack, or requeues it as ``pending``
   with a backoff from :data:`BACKOFF_SCHEDULE_S`; after :data:`MAX_ATTEMPTS`
   it is ``abandoned`` and surfaced by ``janus gateway obligations``.
3. ``replay_once()`` claims due rows (including ``sending`` rows a dead
   process left behind) and sends them through the connected adapters.
   The gateway runs it from a watcher after start-up and on each tick;
   ``janus gateway obligations --replay`` runs it with a standalone sender.

Recording must never break a live send, so every public method swallows its
own storage errors and logs at debug level.

Storage primitives live in :class:`janus_state.SessionDB`; this module owns
the policy. Toggle with ``gateway.delivery_ledger`` (default on — it only
adds durability).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, List, Mapping, Optional

logger = logging.getLogger(__name__)

# Delay before retry n (1-based) of a failed send; the last entry repeats.
BACKOFF_SCHEDULE_S = (30.0, 60.0, 120.0, 300.0, 600.0, 1800.0)
# Total attempts (live send included) before an obligation is abandoned.
MAX_ATTEMPTS = 8
# A reply older than this is stale enough that delivering it would confuse
# more than help; it is abandoned instead of sent.
MAX_AGE_S = 24 * 3600.0
# A row still 'sending' with no update for this long belongs to a process
# that died mid-send and is eligible for replay (at-least-once delivery).
STALE_SENDING_S = 120.0
# How soon to look again when the row's platform has no connected adapter.
NO_ADAPTER_RETRY_S = 30.0
REPLAY_BATCH = 50

Sender = Callable[[Dict[str, Any]], Awaitable[Any]]


def retry_delay(attempts: int) -> Optional[float]:
    """Seconds to wait after ``attempts`` failed tries, or None to abandon."""
    if attempts >= MAX_ATTEMPTS:
        return None
    idx = min(max(attempts, 1) - 1, len(BACKOFF_SCHEDULE_S) - 1)
    return BACKOFF_SCHEDULE_S[idx]


def _find_adapter(adapters: Optional[Mapping[Any, Any]], platform: str) -> Any:
    """Adapters are keyed by ``Platform`` enum members; match on ``.value``."""
    if not adapters:
        return None
    want = (platform or "").strip().lower()
    for key, adapter in adapters.items():
        name = getattr(key, "value", key)
        if str(name).lower() == want:
            return adapter
    return None


class DeliveryLedger:
    """Policy layer over ``SessionDB``'s ``delivery_obligations`` rows."""

    def __init__(self, db: Any, *, clock: Callable[[], float] = time.time, enabled: bool = True):
        self._db = db
        self._clock = clock
        self.enabled = bool(enabled) and db is not None

    @classmethod
    def from_config(cls, db: Any, cfg: Optional[Mapping[str, Any]] = None) -> Optional["DeliveryLedger"]:
        """Build a ledger from ``config.yaml``; None when disabled or no state.db."""
        if db is None:
            return None
        if cfg is None:
            try:
                from janus_cli.config import load_config
                cfg = load_config()
            except Exception:
                cfg = {}
        gateway_cfg = cfg.get("gateway") if isinstance(cfg, Mapping) else None
        enabled = True
        if isinstance(gateway_cfg, Mapping) and "delivery_ledger" in gateway_cfg:
            enabled = bool(gateway_cfg.get("delivery_ledger"))
        if not enabled:
            return None
        return cls(db)

    # ------------------------------------------------------------------
    # Live send path
    # ------------------------------------------------------------------

    def record(
        self,
        *,
        kind: str,
        platform: str,
        chat_id: str,
        content: str,
        thread_id: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Mapping[str, Any]] = None,
        session_id: Optional[str] = None,
        source_ref: Optional[str] = None,
    ) -> Optional[str]:
        """Write the obligation before sending. Returns its id, or None."""
        if not self.enabled or not content:
            return None
        try:
            return self._db.create_obligation(
                kind=kind,
                platform=str(getattr(platform, "value", platform)),
                chat_id=str(chat_id),
                content=content,
                thread_id=str(thread_id) if thread_id is not None else None,
                reply_to=str(reply_to) if reply_to is not None else None,
                metadata=dict(metadata) if metadata else None,
                session_id=session_id,
                source_ref=source_ref,
                now=self._clock(),
            )
        except Exception as exc:
            logger.debug("delivery ledger: record failed: %s", exc)
            return None

    def settle(self, oid: Optional[str], result: Any = None, *, error: Any = None) -> None:
        """Close the loop on a live send: delivered on success, else requeue."""
        if not oid or not self.enabled:
            return
        try:
            if error is None and result is not None and getattr(result, "success", False):
                self._db.settle_obligation_delivered(
                    oid, message_id=getattr(result, "message_id", None), now=self._clock(),
                )
                return
            if error is not None:
                message = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
            else:
                message = (getattr(result, "error", None) or "send failed") if result is not None else "send failed"
            self._fail(oid, message)
        except Exception as exc:
            logger.debug("delivery ledger: settle failed: %s", exc)

    def _fail(self, oid: str, error: str) -> None:
        row = self._db.get_obligation(oid)
        attempts = int((row or {}).get("attempts") or 0) + 1
        delay = retry_delay(attempts)
        now = self._clock()
        retry_at = now + delay if delay is not None else None
        self._db.settle_obligation_failed(oid, error, retry_at=retry_at, now=now)

    # ------------------------------------------------------------------
    # Inspection
    # ------------------------------------------------------------------

    def pending(self, limit: int = 200) -> List[Dict[str, Any]]:
        """Rows waiting for a retry (not the live sends currently in flight)."""
        if not self.enabled:
            return []
        try:
            return self._db.list_obligations(status="pending", limit=limit)
        except Exception:
            return []

    def all(self, *, status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        if not self.enabled:
            return []
        try:
            return self._db.list_obligations(status=status, limit=limit)
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Replay
    # ------------------------------------------------------------------

    async def replay_once(
        self,
        adapters: Optional[Mapping[Any, Any]] = None,
        *,
        send: Optional[Sender] = None,
        now: Optional[float] = None,
        limit: int = REPLAY_BATCH,
    ) -> Dict[str, int]:
        """Send every due obligation once. Returns per-outcome counts.

        ``adapters`` is the gateway's ``{Platform: adapter}`` map; ``send``
        is an alternative coroutine ``send(obligation) -> SendResult`` for
        standalone use. Rows whose platform has no adapter are released to
        be looked at again shortly, without counting an attempt.
        """
        counts = {"delivered": 0, "failed": 0, "skipped": 0, "abandoned": 0}
        if not self.enabled:
            return counts
        ts = self._clock() if now is None else float(now)
        try:
            due = self._db.claim_due_obligations(
                now=ts, limit=limit, stale_sending_after=STALE_SENDING_S,
            )
        except Exception as exc:
            logger.debug("delivery ledger: claim failed: %s", exc)
            return counts

        for ob in due:
            oid = ob["id"]
            try:
                age = ts - float(ob.get("created_at") or ts)
                if age > MAX_AGE_S:
                    self._db.settle_obligation_failed(
                        oid, f"expired: {age / 3600:.1f}h old, not replayed",
                        retry_at=None, now=ts,
                    )
                    counts["abandoned"] += 1
                    continue

                sender = send or self._adapter_sender(adapters, ob.get("platform") or "")
                if sender is None:
                    self._db.release_obligation(oid, retry_at=ts + NO_ADAPTER_RETRY_S, now=ts)
                    counts["skipped"] += 1
                    continue

                try:
                    result = await sender(ob)
                except asyncio.CancelledError:
                    self._db.release_obligation(oid, retry_at=ts, now=ts)
                    raise
                except Exception as exc:
                    self._fail(oid, f"{type(exc).__name__}: {exc}")
                    counts["failed"] += 1
                    continue

                if getattr(result, "success", False):
                    self._db.settle_obligation_delivered(
                        oid, message_id=getattr(result, "message_id", None), now=self._clock(),
                    )
                    counts["delivered"] += 1
                else:
                    self._fail(oid, getattr(result, "error", None) or "send failed")
                    counts["failed"] += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("delivery ledger: replay of %s errored: %s", oid, exc)
        if any(counts.values()):
            logger.info(
                "delivery ledger: replayed %d delivered, %d failed, %d skipped, %d abandoned",
                counts["delivered"], counts["failed"], counts["skipped"], counts["abandoned"],
            )
        return counts

    @staticmethod
    def _adapter_sender(adapters: Optional[Mapping[Any, Any]], platform: str) -> Optional[Sender]:
        adapter = _find_adapter(adapters, platform)
        if adapter is None:
            return None

        async def _send(ob: Dict[str, Any]) -> Any:
            metadata = dict(ob.get("metadata") or {})
            if ob.get("thread_id") and "thread_id" not in metadata:
                metadata["thread_id"] = ob["thread_id"]
            metadata["notify"] = True
            metadata["replayed"] = True
            return await adapter.send(
                ob["chat_id"], ob["content"], reply_to=ob.get("reply_to"), metadata=metadata,
            )

        return _send
