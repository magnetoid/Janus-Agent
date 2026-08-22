"""Ordering/dedup contract for the dashboard session-search endpoint.

Driven through the mounted ``/api/sessions/search`` route rather than by
importing the handler coroutine: the handler has already moved once
(``janus_cli.web_server`` -> ``janus_cli.routers.sessions``) and the route plus
its response shape is what the desktop session picker actually consumes.
"""

import pytest


class _FakeSessionDB:
    """Fake backing the /api/sessions/search endpoint.

    The endpoint surfaces direct session-id matches first, then FTS message
    matches, deduping both by compression lineage root. This fake has no
    compression chains (get_session returns no parent), so each session is its
    own lineage root.
    """

    closed = False

    def search_sessions_by_id(self, query, limit=20, include_archived=True):
        assert query == "20260603"
        assert include_archived is True
        return [
            {
                "id": "20260603_090200_exact",
                "preview": "ID match preview",
                "source": "cli",
                "model": "claude",
                "started_at": 100,
            }
        ]

    def search_messages(self, query, limit=20):
        assert query == "20260603*"
        return [
            {
                "session_id": "20260603_090200_exact",
                "snippet": "duplicate content hit should not replace ID hit",
                "role": "user",
                "source": "cli",
                "model": "claude",
                "session_started": 100,
            },
            {
                "session_id": "content_session",
                "snippet": "content hit",
                "role": "assistant",
                "source": "desktop",
                "model": "gpt",
                "session_started": 200,
            },
        ]

    def get_session(self, session_id):
        # No compression chains in this fixture — every session is its own root.
        return {"id": session_id, "parent_session_id": None}

    def get_compression_tip(self, session_id):
        return session_id

    def close(self):
        self.closed = True


@pytest.fixture()
def client():
    starlette_testclient = pytest.importorskip("starlette.testclient")

    from janus_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    test_client = starlette_testclient.TestClient(app)
    test_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return test_client


def test_desktop_session_search_merges_id_matches_before_content_matches(
    client, monkeypatch
):
    monkeypatch.setattr("janus_state.SessionDB", _FakeSessionDB)

    resp = client.get("/api/sessions/search", params={"q": "20260603", "limit": 2})
    assert resp.status_code == 200, resp.text

    # ID match surfaces first; the content hit on the SAME session is deduped
    # by lineage root (not double-listed); the unrelated content hit follows.
    assert resp.json() == {
        "results": [
            {
                "session_id": "20260603_090200_exact",
                "lineage_root": "20260603_090200_exact",
                "snippet": "ID match preview",
                "role": None,
                "source": "cli",
                "model": "claude",
                "session_started": 100,
            },
            {
                "session_id": "content_session",
                "lineage_root": "content_session",
                "snippet": "content hit",
                "role": "assistant",
                "source": "desktop",
                "model": "gpt",
                "session_started": 200,
            },
        ]
    }
