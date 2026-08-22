"""Regression tests for dashboard cron job profile routing.

These drive the dashboard's public HTTP surface (``/api/cron/jobs*``) rather
than importing the handler coroutines by name. The handlers have moved between
modules before (``janus_cli.web_server`` -> ``janus_cli.routers.cron``) and the
contract the desktop/dashboard depends on is the route + profile query
parameter, not where the coroutine happens to live.
"""

import pytest


@pytest.fixture()
def isolated_profiles(tmp_path, monkeypatch):
    """Give profile discovery an isolated default home with one named profile."""
    from janus_cli import profiles

    default_home = tmp_path / ".janus"
    profiles_root = default_home / "profiles"
    worker_home = profiles_root / "worker_alpha"

    for home in (default_home, worker_home):
        (home / "cron").mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text("model: test-model\n", encoding="utf-8")

    monkeypatch.setattr(profiles, "_get_default_janus_home", lambda: default_home)
    monkeypatch.setattr(profiles, "_get_profiles_root", lambda: profiles_root)
    return {"default": default_home, "worker_alpha": worker_home}


@pytest.fixture()
def client(isolated_profiles):
    """Authenticated dashboard client bound to the isolated profile tree."""
    starlette_testclient = pytest.importorskip("starlette.testclient")

    from janus_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    test_client = starlette_testclient.TestClient(app)
    test_client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    return test_client


def _create_job(client, profile, *, prompt, schedule, name):
    resp = client.post(
        "/api/cron/jobs",
        params={"profile": profile},
        json={"prompt": prompt, "schedule": schedule, "name": name},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _list_jobs(client, profile):
    resp = client.get("/api/cron/jobs", params={"profile": profile})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_call_cron_for_profile_routes_storage_and_restores_globals(isolated_profiles):
    from cron import jobs as cron_jobs
    from janus_cli import web_server

    old_cron_dir = cron_jobs.CRON_DIR
    old_jobs_file = cron_jobs.JOBS_FILE
    old_output_dir = cron_jobs.OUTPUT_DIR

    job = web_server._call_cron_for_profile(
        "worker_alpha",
        "create_job",
        prompt="run scheduled task",
        schedule="every 1h",
        name="worker-alpha-scan",
    )

    assert job["profile"] == "worker_alpha"
    assert job["profile_name"] == "worker_alpha"
    assert job["janus_home"] == str(isolated_profiles["worker_alpha"])
    assert job["is_default_profile"] is False
    assert (isolated_profiles["worker_alpha"] / "cron" / "jobs.json").exists()
    assert not (isolated_profiles["default"] / "cron" / "jobs.json").exists()

    assert cron_jobs.CRON_DIR == old_cron_dir
    assert cron_jobs.JOBS_FILE == old_jobs_file
    assert cron_jobs.OUTPUT_DIR == old_output_dir


def test_list_cron_jobs_all_includes_default_and_named_profiles(isolated_profiles, client):
    default_job = _create_job(
        client,
        "default",
        prompt="default heartbeat",
        schedule="every 2h",
        name="default-heartbeat",
    )
    worker_job = _create_job(
        client,
        "worker_alpha",
        prompt="worker heartbeat",
        schedule="every 3h",
        name="worker-alpha-heartbeat",
    )

    by_id = {job["id"]: job for job in _list_jobs(client, "all")}

    assert set(by_id) >= {default_job["id"], worker_job["id"]}
    assert by_id[default_job["id"]]["profile"] == "default"
    assert by_id[default_job["id"]]["is_default_profile"] is True
    assert by_id[default_job["id"]]["janus_home"] == str(isolated_profiles["default"])
    assert by_id[worker_job["id"]]["profile"] == "worker_alpha"
    assert by_id[worker_job["id"]]["is_default_profile"] is False
    assert by_id[worker_job["id"]]["janus_home"] == str(isolated_profiles["worker_alpha"])


def test_list_cron_jobs_specific_profile_filters_results(isolated_profiles, client):
    _create_job(
        client,
        "default",
        prompt="default only",
        schedule="every 2h",
        name="default-only",
    )
    worker_job = _create_job(
        client,
        "worker_alpha",
        prompt="worker only",
        schedule="every 3h",
        name="worker-only",
    )

    jobs = _list_jobs(client, "worker_alpha")

    assert [job["id"] for job in jobs] == [worker_job["id"]]
    assert jobs[0]["profile"] == "worker_alpha"


def test_cron_mutation_without_profile_finds_named_profile_job(isolated_profiles, client):
    worker_job = _create_job(
        client,
        "worker_alpha",
        prompt="managed by named profile",
        schedule="every 1h",
        name="named-profile-job",
    )

    # No ?profile= — the handler must locate the owning profile itself.
    resp = client.post(f"/api/cron/jobs/{worker_job['id']}/pause")
    assert resp.status_code == 200, resp.text
    paused = resp.json()
    assert paused["profile"] == "worker_alpha"
    assert paused["enabled"] is False

    assert _list_jobs(client, "default") == []
    worker_jobs = _list_jobs(client, "worker_alpha")
    assert len(worker_jobs) == 1
    assert worker_jobs[0]["id"] == worker_job["id"]
    assert worker_jobs[0]["enabled"] is False


def test_update_cron_job_rejects_id_mutation(isolated_profiles, client):
    """Dashboard surfaces a 400 (not a 500 or silent rename) when an
    id-mutation attempt is rejected by cron/jobs.update_job."""
    worker_job = _create_job(
        client,
        "worker_alpha",
        prompt="managed by named profile",
        schedule="every 1h",
        name="immutable-id-job",
    )

    resp = client.put(
        f"/api/cron/jobs/{worker_job['id']}",
        params={"profile": "worker_alpha"},
        json={"updates": {"id": "../escape"}},
    )

    assert resp.status_code == 400
    assert "id" in resp.json()["detail"]
    assert [job["id"] for job in _list_jobs(client, "worker_alpha")] == [worker_job["id"]]


def test_cron_delete_with_profile_deletes_only_target_profile(isolated_profiles, client):
    default_job = _create_job(
        client,
        "default",
        prompt="same-ish default",
        schedule="every 1h",
        name="shared-name",
    )
    worker_job = _create_job(
        client,
        "worker_alpha",
        prompt="same-ish worker",
        schedule="every 1h",
        name="shared-name-worker",
    )

    resp = client.request(
        "DELETE",
        f"/api/cron/jobs/{worker_job['id']}",
        params={"profile": "worker_alpha"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"ok": True}

    assert [job["id"] for job in _list_jobs(client, "default")] == [default_job["id"]]
    assert _list_jobs(client, "worker_alpha") == []


def test_cron_profile_validation_errors(isolated_profiles, client):
    assert client.get("/api/cron/jobs", params={"profile": "../bad"}).status_code == 400
    assert (
        client.get("/api/cron/jobs", params={"profile": "missing_profile"}).status_code
        == 404
    )
