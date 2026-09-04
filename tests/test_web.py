"""The web interface: the guard on the port first, then what the pages show.

The guard tests come first because they are the ones that matter. A run has
every tool pre-granted, so a route that writes is a route that executes code,
and loopback plus no login is safe only while these hold.
"""

import json
import time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from ai_crontab import config, core, tick
from ai_crontab.web import transcript
from ai_crontab.web.app import make_app

# A request the guard should let through, and the one header that earns it.
SAME_SITE = {"host": "127.0.0.1:8474", "x-ai-crontab": "1",
             "origin": "http://127.0.0.1:8474"}


@pytest.fixture
def client(cfg):
    return TestClient(make_app(root=cfg.root), base_url="http://127.0.0.1:8474")


# -- the guard ----------------------------------------------------------

def test_loopback_host_is_accepted(client):
    assert client.get("/", headers={"host": "127.0.0.1:8474"}).status_code == 200
    assert client.get("/", headers={"host": "localhost:8474"}).status_code == 200


def test_other_hosts_are_refused(client):
    """A request that reached us under another name is a rebinding attempt.

    Binding to loopback should make this unreachable; the check is what makes
    that a second lock rather than the only one.
    """
    for host in ["example.com", "192.168.1.10:8474", "ai-crontab.local"]:
        reply = client.get("/", headers={"host": host})
        assert reply.status_code == 403, host


def test_a_write_needs_the_header(client):
    """The header is what a cross-origin page cannot add without a preflight."""
    reply = client.post("/api/state", headers={"host": "127.0.0.1:8474"})
    assert reply.status_code == 403
    assert "X-AI-Crontab" in reply.text


def test_a_write_from_another_site_is_refused(client):
    """A page you visited, posting here, is the threat the design has to stop."""
    reply = client.post("/api/state", headers={
        "host": "127.0.0.1:8474",
        "x-ai-crontab": "1",
        "origin": "https://evil.example",
    })
    assert reply.status_code == 403
    assert "cross-site" in reply.text


def test_reading_never_needs_the_header(client):
    """GET carries no risk, so it must not be made awkward to use."""
    assert client.get("/inbox", headers={"host": "localhost"}).status_code == 200


# -- the transcript reader ----------------------------------------------

def write_transcript(path, events, tail=""):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for event in events:
            f.write(json.dumps(event) + "\n")
        f.write(tail)


def test_transcript_reads_a_whole_run(tmp_path):
    path = tmp_path / "r_1.jsonl"
    write_transcript(path, [
        {"type": "system", "subtype": "init", "cwd": "/home/x"},
        {"type": "assistant", "message": {"model": "claude-opus-5", "content": [
            {"type": "text", "text": "checking the queue"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "curl ..."}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "3 bugs"}]}},
        {"type": "result", "subtype": "success", "total_cost_usd": 0.31,
         "num_turns": 6, "duration_ms": 50123, "is_error": False,
         "result": "filed one notification",
         "usage": {"input_tokens": 18, "cache_read_input_tokens": 184113,
                   "output_tokens": 5527,
                   "output_tokens_details": {"thinking_tokens": 882}}},
    ])
    parsed = transcript.read(path)
    assert [r.kind for r in parsed.rows] == ["text", "tool_use", "tool_result"]
    assert parsed.rows[1].title == "Bash"
    assert parsed.model == "claude-opus-5"
    assert parsed.cwd == "/home/x"
    assert parsed.num_turns == 6
    assert parsed.cost_usd == 0.31
    assert parsed.usage["cache_read"] == 184113
    assert parsed.usage["thinking"] == 882
    assert parsed.finished


def test_a_killed_run_leaves_a_partial_line(tmp_path):
    """The run someone most wants to read is the one that was killed.

    A truncated final line must cost the last row and nothing else, and must be
    reported rather than passed off as a complete transcript.
    """
    path = tmp_path / "r_2.jsonl"
    write_transcript(path, [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "still working"}]}},
    ], tail='{"type": "assistant", "message": {"cont')
    parsed = transcript.read(path)
    assert parsed.truncated
    assert not parsed.finished
    assert parsed.num_turns is None
    assert [r.kind for r in parsed.rows] == ["text"]


def test_a_run_with_no_transcript_still_reads(tmp_path):
    """An agent killed before it spoke leaves no file, and no exception."""
    parsed = transcript.read(tmp_path / "never-written.jsonl")
    assert parsed.rows == []
    assert not parsed.truncated


def test_tool_results_come_in_two_shapes(tmp_path):
    """One tool returns a string, another a list of blocks. Both must show."""
    path = tmp_path / "r_3.jsonl"
    write_transcript(path, [
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "plain text"}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": [{"type": "text", "text": "in a block"}]}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "no such file", "is_error": True}]}},
    ])
    rows = transcript.read(path).rows
    assert rows[0].body == "plain text"
    assert rows[1].body == "in a block"
    assert rows[2].is_error


def test_empty_thinking_is_not_a_row(tmp_path):
    """Thinking arrives as a signature with no text, so there is nothing to show."""
    path = tmp_path / "r_4.jsonl"
    write_transcript(path, [
        {"type": "assistant", "message": {"content": [
            {"type": "thinking", "thinking": "", "signature": "abc"},
            {"type": "text", "text": "done"}]}},
    ])
    assert [r.kind for r in transcript.read(path).rows] == ["text"]


def test_a_long_body_is_clipped(tmp_path):
    path = tmp_path / "r_5.jsonl"
    write_transcript(path, [
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "x" * 9000}]}},
    ])
    body = transcript.read(path, max_body=100).rows[0].body
    assert len(body) < 200
    assert body.endswith("(clipped)")


# -- the pages ----------------------------------------------------------

def test_dashboard_lists_tasks(client, make_task):
    task = make_task(name="gtk-triage-watch")
    body = client.get("/", headers=SAME_SITE).text
    assert "gtk-triage-watch" in body
    assert task.schedule in body


def test_dashboard_says_when_there_is_nothing(client):
    assert "No tasks" in client.get("/", headers=SAME_SITE).text


def test_health_reports_a_missing_tick(client, cfg, store):
    """Before the first tick there is nothing to compare against, and that is
    not the same as the scheduler being broken."""
    assert "No tick recorded" in client.get("/", headers=SAME_SITE).text


def test_health_notices_a_late_scheduler(client, store, cfg):
    late = config.utcnow() - timedelta(seconds=cfg.tick_interval_sec * 5)
    store.set_meta("last_tick", late.isoformat())
    assert "longer than" in client.get("/", headers=SAME_SITE).text


def test_health_is_quiet_when_the_scheduler_is_alive(client, store):
    store.set_meta("last_tick", config.utcnow().isoformat())
    body = client.get("/", headers=SAME_SITE).text
    assert "ticked" in body and "longer than" not in body


def test_task_page_shows_the_prompt_and_handoff(client, cfg, make_task):
    task = make_task(name="watcher")
    core.write_handoff(cfg, task.id, "stopped at bug 1621261")
    body = client.get(f"/tasks/{task.id}", headers=SAME_SITE).text
    assert "stopped at bug 1621261" in body
    assert task.prompt in body


def test_a_task_can_be_reached_by_name(client, make_task):
    """Every other door takes an id or a name, so this one does too."""
    make_task(name="by-name")
    assert client.get("/tasks/by-name", headers=SAME_SITE).status_code == 200


def test_an_unknown_task_is_a_404(client):
    assert client.get("/tasks/nope", headers=SAME_SITE).status_code == 404


def test_run_page_renders_the_transcript(client, cfg, store, make_task):
    task = make_task(name="ran-once")
    run_id = store.claim_run(task.id, config.utcnow())
    store.finish_run(run_id, "ok", config.utcnow(), exit_code=0)
    write_transcript(cfg.transcript_path(task.id, run_id), [
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "WebSearch", "input": {"query": "tsla"}}]}},
        {"type": "result", "num_turns": 4, "total_cost_usd": 0.62,
         "result": "reported"},
    ])
    body = client.get(f"/runs/{run_id}", headers=SAME_SITE).text
    assert "WebSearch" in body
    assert "reported" in body
    assert "$0.6200" in body


def test_run_page_says_when_a_run_never_finished(client, cfg, store, make_task):
    task = make_task(name="killed")
    run_id = store.claim_run(task.id, config.utcnow())
    store.finish_run(run_id, "timeout", config.utcnow(), error="ran past its limit")
    body = client.get(f"/runs/{run_id}", headers=SAME_SITE).text
    assert "did not finish on its own" in body
    assert "ran past its limit" in body


def test_an_unknown_run_is_a_404(client):
    assert client.get("/runs/r_nope", headers=SAME_SITE).status_code == 404


def test_inbox_shows_unread_and_hides_read(client, cfg, store, make_task):
    task = make_task(name="noisy")
    core.notify(store, task.id, None, "queue grew", "three new bugs")
    body = client.get("/inbox", headers=SAME_SITE).text
    assert "queue grew" in body and "three new bugs" in body

    store.mark_all_read(config.utcnow())
    assert "queue grew" not in client.get("/inbox", headers=SAME_SITE).text
    assert "queue grew" in client.get("/inbox?all", headers=SAME_SITE).text


def test_a_quiet_inbox_says_so(client):
    """Silence is the normal result, so the empty state must not read as a fault."""
    body = client.get("/inbox", headers=SAME_SITE).text
    assert "Nothing unread" in body
    assert "files nothing" in body


def test_api_state_is_json(client, store, make_task):
    make_task(name="polled")
    data = client.get("/api/state", headers=SAME_SITE).json()
    assert data["tasks"][0]["name"] == "polled"
    assert data["unread"] == 0
    assert data["health"]["state"] == "unknown"


def test_config_is_re_read_between_requests(client, cfg):
    """A server left up for a week has to notice an edit to config.toml."""
    (cfg.root / "config.toml").write_text("tick_interval_sec = 60\n")
    assert client.get("/", headers=SAME_SITE).status_code == 200
    (cfg.root / "config.toml").write_text("tick_interval_sec = 900\n")
    body = client.get("/", headers=SAME_SITE).text
    assert "15 min interval" in body or "No tick recorded" in body


# -- what a write refuses -----------------------------------------------
#
# SC-18. These matter more than the ones below them: a task is a prompt run
# with every tool pre-granted, so a route that creates one is a route that runs
# code on this machine, and the only thing standing between that and a page you
# have open in another tab is what these assert.

NEW_TASK = {"name": "from-the-browser", "prompt": "quiet", "schedule": "0 9 * * *"}


def test_a_foreign_page_cannot_create_a_task(client, store):
    reply = client.post("/tasks/new", data=NEW_TASK, headers={
        "host": "127.0.0.1:8474",
        "x-ai-crontab": "1",
        "origin": "https://evil.example",
    })
    assert reply.status_code == 403
    assert store.find_task("from-the-browser") is None


def test_a_form_post_without_the_header_cannot_create_a_task(client, store):
    """A plain HTML form is exactly what a foreign page can send, and cannot
    add a header to. That is why the interface posts with fetch instead."""
    reply = client.post("/tasks/new", data=NEW_TASK,
                        headers={"host": "127.0.0.1:8474"})
    assert reply.status_code == 403
    assert store.find_task("from-the-browser") is None


def test_the_interface_can_create_a_task(client, store):
    reply = client.post("/tasks/new", data=NEW_TASK, headers=SAME_SITE)
    assert reply.status_code == 200
    task = store.find_task("from-the-browser")
    assert task is not None
    assert reply.json()["redirect"] == f"/tasks/{task.id}"


# -- the writes ---------------------------------------------------------

def test_a_new_task_takes_the_defaults_it_was_not_given(client, cfg, store):
    client.post("/tasks/new", data=NEW_TASK, headers=SAME_SITE)
    task = store.find_task("from-the-browser")
    assert task.time_limit_sec == cfg.time_limit_sec
    assert task.enabled
    assert task.next_run is not None


def test_a_duplicate_name_is_answered_not_raised(client, make_task):
    """The CLI answers this with one line, so the browser gets the same line."""
    make_task(name="taken")
    reply = client.post("/tasks/new", headers=SAME_SITE,
                        data={**NEW_TASK, "name": "taken"})
    assert reply.status_code == 400
    assert "already exists" in reply.json()["detail"]


def test_a_schedule_that_does_not_parse_is_answered_not_raised(client):
    reply = client.post("/tasks/new", headers=SAME_SITE,
                        data={**NEW_TASK, "schedule": "every other tuesday"})
    assert reply.status_code == 400
    assert "not a cron expression" in reply.json()["detail"]


def test_a_missing_field_is_a_400(client):
    reply = client.post("/tasks/new", headers=SAME_SITE,
                        data={"name": "nameless", "schedule": "0 9 * * *"})
    assert reply.status_code == 400
    assert "prompt is required" in reply.json()["detail"]


def test_editing_changes_only_what_the_form_carried(client, store, make_task):
    task = make_task(name="editable")
    client.post(f"/tasks/{task.id}", headers=SAME_SITE,
                data={"prompt": "quiet, but rewritten"})
    changed = store.get_task(task.id)
    assert changed.prompt == "quiet, but rewritten"
    assert changed.name == "editable"
    assert changed.schedule == task.schedule


def test_a_new_schedule_moves_the_next_run(client, store, make_task):
    task = make_task(name="rescheduled", schedule="*/5 * * * *")
    client.post(f"/tasks/{task.id}", headers=SAME_SITE, data={"schedule": "0 9 * * *"})
    changed = store.get_task(task.id)
    assert changed.schedule == "0 9 * * *"
    assert changed.next_run != task.next_run


def test_disabling_and_enabling_are_the_same_route(client, store, make_task):
    """Both are one field on the task, so both are `modify`, as in the CLI."""
    task = make_task(name="switchable")
    client.post(f"/tasks/{task.id}", headers=SAME_SITE, data={"enabled": "false"})
    off = store.get_task(task.id)
    assert not off.enabled and off.next_run is None

    client.post(f"/tasks/{task.id}", headers=SAME_SITE, data={"enabled": "true"})
    on = store.get_task(task.id)
    assert on.enabled and on.next_run is not None


def test_deleting_a_task_takes_its_directory_with_it(client, cfg, store, make_task):
    task = make_task(name="doomed")
    core.write_handoff(cfg, task.id, "something to lose")
    reply = client.post(f"/tasks/{task.id}/delete", headers=SAME_SITE)
    assert reply.json()["redirect"] == "/"
    assert store.get_task(task.id) is None
    assert not cfg.task_dir(task.id).exists()


def test_a_write_to_an_unknown_task_is_a_404(client):
    assert client.post("/tasks/nope", headers=SAME_SITE,
                       data={"enabled": "false"}).status_code == 404


def test_run_now_starts_a_run(client, store, make_task, monkeypatch):
    started = _record_spawns(monkeypatch)
    task = make_task(name="impatient")
    reply = client.post(f"/tasks/{task.id}/run", headers=SAME_SITE)
    assert reply.status_code == 200
    assert store.count_active_runs() == 1
    assert reply.json()["redirect"] == f"/runs/{started[0]}"


def test_run_now_declines_when_a_run_is_already_in_flight(client, store,
                                                          make_task, monkeypatch):
    """The overlap rule is the store's, not the interface's: `claim_run`
    refuses, and 409 is that refusal said in HTTP."""
    _record_spawns(monkeypatch)
    task = make_task(name="already-going")
    client.post(f"/tasks/{task.id}/run", headers=SAME_SITE)
    assert client.post(f"/tasks/{task.id}/run",
                       headers=SAME_SITE).status_code == 409
    assert store.count_active_runs() == 1


def _record_spawns(monkeypatch):
    """Note which runs would have been launched, without launching one.

    A route handler runs on a thread of its own, so a stand-in that touched the
    test's own store connection would fail on SQLite's thread check rather than
    on anything the route did wrong.
    """
    started = []
    monkeypatch.setattr("ai_crontab.tick.spawn_supervisor",
                        lambda cfg, run_id: started.append(run_id))
    return started


def test_a_notification_can_be_marked_read_and_deleted(client, store, make_task):
    task = make_task(name="chatty")
    first = core.notify(store, task.id, None, "one")
    second = core.notify(store, task.id, None, "two")

    client.post(f"/inbox/{first}", headers=SAME_SITE, data={"action": "read"})
    assert not store.get_notification(first).is_unread
    assert store.unread_count() == 1

    client.post(f"/inbox/{second}", headers=SAME_SITE, data={"action": "delete"})
    assert store.get_notification(second) is None


def test_the_whole_inbox_can_be_marked_read(client, store, make_task):
    task = make_task(name="loud")
    core.notify(store, task.id, None, "one")
    core.notify(store, task.id, None, "two")
    reply = client.post("/inbox", headers=SAME_SITE)
    assert store.unread_count() == 0
    assert "2 marked read" in reply.json()["message"]


def test_a_write_to_an_unknown_notification_is_a_404(client):
    assert client.post("/inbox/999", headers=SAME_SITE,
                       data={"action": "delete"}).status_code == 404


# -- the same run down every door ---------------------------------------

def test_the_environment_a_run_gets_is_built_not_inherited(cfg, monkeypatch):
    """`child_env` has to be a function of the config, or SC-20 cannot hold.

    The variable below is the shape of the real failure: something a login
    shell has and cron does not, reaching a run only when you start it by hand.
    """
    monkeypatch.setenv("SOMETHING_ONLY_A_TERMINAL_HAS", "1")
    monkeypatch.setenv("PATH", "/nowhere")
    env = tick.child_env(cfg)
    assert "SOMETHING_ONLY_A_TERMINAL_HAS" not in env
    assert env["PATH"] == cfg.run_path
    assert env["AI_CRONTAB_ROOT"] == str(cfg.root)
    assert env["HOME"]


def test_a_run_from_the_browser_matches_one_from_a_tick(client, cfg, store,
                                                        make_task, monkeypatch):
    """SC-20, with two real runs rather than an argument about the code.

    The task writes down its own environment, and the two copies have to match
    exactly. This is the session-3 `claude_bin` failure in its second hat: the
    server is started from a login shell and cron is not, so a run that
    inherited its parent's environment would resolve a different `claude`
    depending on which of these two lines started it.

    The environment is therefore changed between the two, into roughly the
    difference a real machine has between the two doors. A run that carried any
    of that difference through would fail here.
    """
    task = make_task(mode="envdump", name="records-its-environment")
    env_file = cfg.scratch_dir(task.id) / "env.json"

    monkeypatch.setenv("SOMETHING_ONLY_A_TERMINAL_HAS", "1")
    run_id = client.post(f"/tasks/{task.id}/run",
                         headers=SAME_SITE).json()["redirect"].rsplit("/", 1)[1]
    _wait_for_run(store, run_id)
    from_the_browser = json.loads(env_file.read_text())

    env_file.unlink()
    monkeypatch.delenv("SOMETHING_ONLY_A_TERMINAL_HAS")
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    _make_due(store, task.id)
    started = tick.tick(cfg, store)
    assert len(started) == 1
    _wait_for_run(store, started[0])
    from_a_tick = json.loads(env_file.read_text())

    assert from_the_browser == from_a_tick
    assert from_the_browser["PATH"] == cfg.run_path


def _make_due(store, task_id):
    """Put a task back in the past, so the next tick finds it due."""
    task = store.get_task(task_id)
    task.next_run = config.utcnow() - timedelta(seconds=1)
    store.update_task(task)


def _wait_for_run(store, run_id, timeout=30):
    """Block until a real supervisor has finished with a run.

    These two runs are launched the way cron launches one, as a detached
    process, so there is nothing to wait on but the ending the supervisor is
    required to record.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        run = store.get_run(run_id)
        if run is not None and run.state != "active":
            return run
        time.sleep(0.1)
    raise AssertionError(f"run {run_id} never finished")


def test_a_blank_field_leaves_the_stored_value_alone(client, store, make_task):
    """A half-filled form must not be a way to end up with an empty prompt."""
    task = make_task(name="intact")
    client.post(f"/tasks/{task.id}", headers=SAME_SITE,
                data={"name": "", "prompt": "", "working_dir": "",
                      "schedule": "", "time_limit_sec": ""})
    unchanged = store.get_task(task.id)
    assert unchanged.name == "intact"
    assert unchanged.prompt == task.prompt
    assert unchanged.working_dir == task.working_dir
    assert unchanged.time_limit_sec == task.time_limit_sec


def test_marking_an_already_read_notification_read_is_not_an_error(client, store,
                                                                   make_task):
    """Two tabs open on the same inbox is an ordinary way to arrive twice."""
    task = make_task(name="twice")
    note_id = core.notify(store, task.id, None, "read me")
    for _ in range(2):
        reply = client.post(f"/inbox/{note_id}", headers=SAME_SITE,
                            data={"action": "read"})
        assert reply.status_code == 200


def test_an_action_the_inbox_does_not_have_is_refused(client, store, make_task):
    task = make_task(name="picky")
    note_id = core.notify(store, task.id, None, "leave me alone")
    reply = client.post(f"/inbox/{note_id}", headers=SAME_SITE,
                        data={"action": "burn"})
    assert reply.status_code == 400
    assert store.get_notification(note_id) is not None


# -- watching a run happen ----------------------------------------------
#
# SC-21. Two mechanisms, and the risk in each is the same one: the page and the
# poll must agree about what the page is showing. The schedule agrees through a
# signature built from the very rows it rendered; a run's page agrees through a
# row count, which is only a safe cursor for as long as rows are append-only.

def test_the_page_and_the_poll_sign_the_same_state(client, make_task):
    """The signature on the page is the one the poll compares against.

    Two ways of gathering the same numbers would let the page render one state
    and the poll describe another, and the symptom would be a page that
    refreshes forever.
    """
    make_task(name="signed")
    page = client.get("/", headers=SAME_SITE).text
    signature = client.get("/api/state", headers=SAME_SITE).json()["signature"]
    assert f'data-signature="{signature}"' in page


def test_the_signature_holds_still_when_nothing_happens(client, make_task):
    """Elapsed time must not be in it, or the page would refresh every second."""
    make_task(name="quiet")
    first = client.get("/api/state", headers=SAME_SITE).json()["signature"]
    time.sleep(1.1)
    assert client.get("/api/state", headers=SAME_SITE).json()["signature"] == first


@pytest.mark.parametrize("change", ["run", "notification", "disable", "edit"])
def test_the_signature_moves_when_the_page_would(client, store, make_task, change):
    task = make_task(name="watched")
    before = client.get("/api/state", headers=SAME_SITE).json()["signature"]

    if change == "run":
        store.claim_run(task.id, config.utcnow())
    elif change == "notification":
        core.notify(store, task.id, None, "something", "happened")
    elif change == "disable":
        client.post(f"/tasks/{task.id}", data={"enabled": "false"}, headers=SAME_SITE)
    else:
        client.post(f"/tasks/{task.id}", data={"name": "renamed"}, headers=SAME_SITE)

    assert client.get("/api/state", headers=SAME_SITE).json()["signature"] != before


def test_a_run_in_flight_is_named_with_the_time_it_started(client, store, make_task):
    """The clock is drawn in the browser, so the poll has to say from when."""
    task = make_task(name="running")
    run_id = store.claim_run(task.id, config.utcnow())
    state = client.get("/api/state", headers=SAME_SITE).json()
    assert [r["run"] for r in state["active"]] == [run_id]
    assert state["active"][0]["started"] is not None


def test_the_tail_starts_with_everything(client, cfg, store, make_task):
    task = make_task(name="tailed")
    run_id = store.claim_run(task.id, config.utcnow())
    write_transcript(cfg.transcript_path(task.id, run_id), [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "first"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "date"}}]}},
    ])
    tail = client.get(f"/runs/{run_id}/rows", headers=SAME_SITE).json()
    assert tail["count"] == 2
    assert tail["active"] is True
    assert "first" in tail["html"] and "Bash" in tail["html"]


def test_the_tail_sends_only_what_the_page_has_not_got(client, cfg, store,
                                                       make_task):
    task = make_task(name="partly-read")
    run_id = store.claim_run(task.id, config.utcnow())
    write_transcript(cfg.transcript_path(task.id, run_id), [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "already shown"}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "the new one"}]}},
    ])
    tail = client.get(f"/runs/{run_id}/rows?start=1", headers=SAME_SITE).json()
    assert "already shown" not in tail["html"]
    assert "the new one" in tail["html"]
    assert tail["count"] == 2


def test_a_growing_transcript_never_repeats_or_skips_a_row(client, cfg, store,
                                                           make_task):
    """The one thing paging by row index depends on.

    A file being appended to while it is read ends mid-line, and the reader
    yields no row for that line. When the rest of it lands the row appears
    once, after the rows already sent and before anything written later, so
    following the tail reconstructs the transcript exactly.
    """
    task = make_task(name="growing")
    run_id = store.claim_run(task.id, config.utcnow())
    path = cfg.transcript_path(task.id, run_id)
    turn = '{"type": "assistant", "message": {"content": [{"type": "text", "text": "%s"}]}}\n'

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(turn % "one" + (turn % "two")[:40])

    seen, start = [], 0
    for more in ((turn % "two")[40:] + (turn % "three"), turn % "four"):
        tail = client.get(f"/runs/{run_id}/rows?start={start}",
                          headers=SAME_SITE).json()
        seen.append(tail["html"])
        start = tail["count"]
        with open(path, "a") as f:
            f.write(more)

    tail = client.get(f"/runs/{run_id}/rows?start={start}", headers=SAME_SITE).json()
    seen.append(tail["html"])
    whole = "".join(seen)
    assert [whole.count(word) for word in ("one", "two", "three", "four")] == [1, 1, 1, 1]
    assert whole.index("one") < whole.index("two") < whole.index("three") \
        < whole.index("four")


def test_the_tail_reads_the_end_from_the_run_not_the_transcript(client, cfg,
                                                                store, make_task):
    """A killed run never writes the `result` event that would mark it done.

    Believing the transcript would leave the page polling a run that ended
    minutes ago, which is the state this whole mechanism exists to avoid being
    stuck in.
    """
    task = make_task(name="killed-mid-sentence")
    run_id = store.claim_run(task.id, config.utcnow())
    write_transcript(cfg.transcript_path(task.id, run_id), [
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "working"}]}},
    ], tail='{"type": "assist')
    assert client.get(f"/runs/{run_id}/rows", headers=SAME_SITE).json()["active"]

    store.finish_run(run_id, "timeout", config.utcnow(), error="ran past its limit")
    tail = client.get(f"/runs/{run_id}/rows", headers=SAME_SITE).json()
    assert tail["active"] is False
    assert tail["state"] == "timeout"


def test_the_tail_renders_what_the_page_renders(client, cfg, store, make_task):
    """One description of a turn, not two.

    The page includes the same template the tail renders on its own, so a row
    that arrives while you watch is the row you would have got by reloading.
    """
    task = make_task(name="one-renderer")
    run_id = store.claim_run(task.id, config.utcnow())
    write_transcript(cfg.transcript_path(task.id, run_id), [
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "content": "boom", "is_error": True}]}},
    ])
    page = client.get(f"/runs/{run_id}", headers=SAME_SITE).text
    tail = client.get(f"/runs/{run_id}/rows", headers=SAME_SITE).json()["html"]
    assert tail.strip() in page


def test_the_run_page_only_asks_for_a_tail_while_the_run_is_going(client, cfg,
                                                                  store, make_task):
    task = make_task(name="finished")
    run_id = store.claim_run(task.id, config.utcnow())
    assert 'data-live="1"' in client.get(f"/runs/{run_id}", headers=SAME_SITE).text

    store.finish_run(run_id, "ok", config.utcnow(), exit_code=0)
    assert 'data-live="1"' not in client.get(f"/runs/{run_id}", headers=SAME_SITE).text


def test_a_run_with_nothing_yet_is_not_a_run_that_said_nothing(client, store,
                                                               make_task):
    """The same empty list means two different things, and only one is bad."""
    task = make_task(name="just-started")
    run_id = store.claim_run(task.id, config.utcnow())
    assert "has not finished its first turn" in \
        client.get(f"/runs/{run_id}", headers=SAME_SITE).text

    store.finish_run(run_id, "timeout", config.utcnow())
    assert "killed before it spoke" in \
        client.get(f"/runs/{run_id}", headers=SAME_SITE).text


def test_a_tail_of_an_unknown_run_is_a_404(client):
    assert client.get("/runs/r_nope/rows", headers=SAME_SITE).status_code == 404


def test_the_health_line_counts_in_the_browser(client, store):
    """"Ticked 42s ago" would be a lie that grew for the five minutes between
    ticks, so the number is a clock the page draws rather than a rendered one."""
    store.set_meta("last_tick", config.utcnow().isoformat())
    body = client.get("/", headers=SAME_SITE).text
    assert 'class="elapsed" data-since=' in body
    assert "s ago" not in body
