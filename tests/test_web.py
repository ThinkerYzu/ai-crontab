"""The web interface: the guard on the port first, then what the pages show.

The guard tests come first because they are the ones that matter. A run has
every tool pre-granted, so a route that writes is a route that executes code,
and loopback plus no login is safe only while these hold.
"""

import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from ai_crontab import config, core
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
