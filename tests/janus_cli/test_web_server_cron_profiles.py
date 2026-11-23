"""Regression tests for dashboard cron job profile routing.

These drive the dashboard's public HTTP surface (``/api/cron/jobs*``) rather
than importing the handler coroutines by name. The handlers have moved between
modules before (``janus_cli.web_server`` -> ``janus_cli.routers.cron``) and the
contract the desktop/dashboard depends on is the route + profile query
parameter, not where the coroutine happens to live.
"""

import json
import threading

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


def _job_names(jobs_file):
    return [job["name"] for job in json.loads(jobs_file.read_text(encoding="utf-8"))["jobs"]]


def test_dashboard_profile_call_leaves_concurrent_ticker_on_its_own_home(
    isolated_profiles, monkeypatch
):
    """A dashboard call for another profile must not redirect the ticker.

    The desktop backend runs the cron ticker in the same process as the
    dashboard routes. Here a ticker-style ``mark_job_run`` for a default-profile
    job loads jobs.json, then parks until a dashboard call for worker_alpha is
    in progress, and only then saves. Its save, a later read and its job output
    must all still land in the default profile, and worker_alpha's files must
    stay as they were.
    """
    from cron import jobs as cron_jobs
    from janus_cli import web_server

    default_cron = isolated_profiles["default"] / "cron"
    worker_cron = isolated_profiles["worker_alpha"] / "cron"
    # The ticker uses the process's own home; make that the default profile,
    # as it is for a desktop app running the default profile.
    monkeypatch.setattr(cron_jobs, "CRON_DIR", default_cron)
    monkeypatch.setattr(cron_jobs, "JOBS_FILE", default_cron / "jobs.json")
    monkeypatch.setattr(cron_jobs, "OUTPUT_DIR", default_cron / "output")

    default_job = cron_jobs.create_job(
        prompt="default heartbeat", schedule="every 1h", name="default-job"
    )
    worker_job = web_server._call_cron_for_profile(
        "worker_alpha",
        "create_job",
        prompt="worker heartbeat",
        schedule="every 1h",
        name="worker-job",
    )
    worker_jobs_before = (worker_cron / "jobs.json").read_bytes()

    ticker_loaded = threading.Event()
    dashboard_inside = threading.Event()
    ticker_done = threading.Event()
    ticker_state = {}

    real_compute_next_run = cron_jobs.compute_next_run

    def compute_next_run_parked(*args, **kwargs):
        # mark_job_run calls this after load_jobs() and before save_jobs().
        if threading.current_thread().name == "ticker":
            ticker_loaded.set()
            ticker_state["overlapped"] = dashboard_inside.wait(timeout=5)
        return real_compute_next_run(*args, **kwargs)

    def ticker():
        try:
            cron_jobs.mark_job_run(default_job["id"], True)
            ticker_state["read"] = [job["name"] for job in cron_jobs.load_jobs()]
            ticker_state["output"] = cron_jobs.save_job_output(
                default_job["id"], "default output"
            )
        except BaseException as exc:  # surfaced by the assertions below
            ticker_state["error"] = exc
        finally:
            ticker_done.set()

    real_list_jobs = cron_jobs.list_jobs

    def list_jobs_parked(*args, **kwargs):
        result = real_list_jobs(*args, **kwargs)
        dashboard_inside.set()
        ticker_done.wait(timeout=5)
        return result

    monkeypatch.setattr(cron_jobs, "compute_next_run", compute_next_run_parked)
    monkeypatch.setattr(cron_jobs, "list_jobs", list_jobs_parked)

    thread = threading.Thread(target=ticker, name="ticker", daemon=True)
    thread.start()
    assert ticker_loaded.wait(timeout=5)
    listed = web_server._call_cron_for_profile("worker_alpha", "list_jobs", True)
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert ticker_state.get("error") is None
    assert ticker_state["overlapped"] is True

    # worker_alpha's jobs.json was not overwritten, and got no default output.
    assert _job_names(worker_cron / "jobs.json") == ["worker-job"]
    assert (worker_cron / "jobs.json").read_bytes() == worker_jobs_before
    assert not (worker_cron / "output" / default_job["id"]).exists()

    # The ticker's write, read and output all hit the default profile.
    [saved_default] = json.loads((default_cron / "jobs.json").read_text(encoding="utf-8"))["jobs"]
    assert saved_default["id"] == default_job["id"]
    assert saved_default["last_run_at"]
    assert saved_default["last_status"] == "ok"
    assert ticker_state["read"] == ["default-job"]
    assert ticker_state["output"].parent == default_cron / "output" / default_job["id"]

    # The dashboard call itself still served worker_alpha.
    assert [job["id"] for job in listed] == [worker_job["id"]]
    assert {job["profile"] for job in listed} == {"worker_alpha"}


def test_dashboard_profile_calls_use_that_profiles_output_dir(isolated_profiles, client):
    """Output reads, writes and deletes from the dashboard go to the profile's own dir."""
    from janus_cli import web_server

    default_job = _create_job(
        client, "default", prompt="default job", schedule="every 1h", name="default-out"
    )
    worker_job = _create_job(
        client, "worker_alpha", prompt="worker job", schedule="every 1h", name="worker-out"
    )
    for profile, job in (("default", default_job), ("worker_alpha", worker_job)):
        web_server._call_cron_for_profile(profile, "save_job_output", job["id"], f"{profile} output")

    worker_output = isolated_profiles["worker_alpha"] / "cron" / "output" / worker_job["id"]
    default_output = isolated_profiles["default"] / "cron" / "output" / default_job["id"]
    assert worker_output.is_dir()
    assert default_output.is_dir()
    assert web_server._call_cron_for_profile(
        "worker_alpha", "latest_job_output", worker_job["id"]
    ) == "worker_alpha output"

    resp = client.request(
        "DELETE", f"/api/cron/jobs/{worker_job['id']}", params={"profile": "worker_alpha"}
    )
    assert resp.status_code == 200, resp.text

    assert not worker_output.exists()
    assert default_output.is_dir()
    assert _job_names(isolated_profiles["default"] / "cron" / "jobs.json") == ["default-out"]
    assert _job_names(isolated_profiles["worker_alpha"] / "cron" / "jobs.json") == []


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
