"""SessionDB reads must not race ``close()`` on the shared connection.

The connection is opened with ``check_same_thread=False`` and shared by
background threads (the delegation stop-poller, the gateway handoff watcher).
``close()`` takes ``self._lock``; any read that skips the lock can be inside
``sqlite3.Connection.execute`` on one thread while another thread closes the
connection, and SQLite aborts or segfaults the whole process
(``Fatal Python error: Aborted`` in tests/tools/test_delegate_background.py).

Each reader below is parked inside ``execute`` with a proxy connection;
``close()`` must wait for it instead of closing underneath it.
"""

from __future__ import annotations

import threading

import pytest

from janus_state import SessionDB


class _ParkingConn:
    """Forwards to the real connection; parks the first matching SELECT."""

    def __init__(self, real, marker: str):
        self._real = real
        self._marker = marker
        self.entered = threading.Event()
        self.release = threading.Event()
        self.closed_while_reading = False
        self._reading = False

    def execute(self, sql, *args, **kwargs):
        if self._marker in sql and not self.entered.is_set():
            self._reading = True
            self.entered.set()
            self.release.wait(5)
            try:
                return self._real.execute(sql, *args, **kwargs)
            finally:
                self._reading = False
        return self._real.execute(sql, *args, **kwargs)

    def close(self):
        if self._reading:
            self.closed_while_reading = True
        self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


READERS = [
    ("get_delegation", lambda db: db.get_delegation("d-1"), "FROM delegations WHERE id"),
    ("list_delegations", lambda db: db.list_delegations(), "FROM delegations"),
    ("running_delegation_pids", lambda db: db.running_delegation_pids(), "SELECT DISTINCT pid"),
    ("get_handoff_state", lambda db: db.get_handoff_state("s-1"), "handoff_state, handoff_platform"),
    ("list_pending_handoffs", lambda db: db.list_pending_handoffs(), "handoff_state = 'pending'"),
    ("get_compression_lock_holder", lambda db: db.get_compression_lock_holder("s-1"), "FROM compression_locks"),
]


@pytest.mark.parametrize(("name", "read", "marker"), READERS, ids=[r[0] for r in READERS])
def test_close_waits_for_an_in_flight_read(tmp_path, name, read, marker):
    db = SessionDB(db_path=tmp_path / "state.db")
    proxy = _ParkingConn(db._conn, marker)
    db._conn = proxy

    reader = threading.Thread(target=read, args=(db,), daemon=True)
    reader.start()
    assert proxy.entered.wait(5), f"{name} never reached execute()"

    closer = threading.Thread(target=db.close, daemon=True)
    closer.start()
    closer.join(0.3)
    closed_early = not closer.is_alive()

    proxy.release.set()
    reader.join(5)
    closer.join(5)

    assert not closed_early, f"close() did not wait for the in-flight {name}()"
    assert not proxy.closed_while_reading


def test_reads_after_close_fail_cleanly(tmp_path):
    """A poller that outlives the DB gets an ordinary exception, not a crash."""
    db = SessionDB(db_path=tmp_path / "state.db")
    db.close()
    with pytest.raises(Exception):
        db.get_delegation("d-1")
    assert db.list_pending_handoffs() == []
    assert db.get_handoff_state("s-1") is None
