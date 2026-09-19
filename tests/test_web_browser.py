"""What a write looks like in a browser, which is the only place it can be seen.

Every other web test asserts on the reply a route sends. That is where this
family of bug hides: a route can answer perfectly and the page still not
change, because what the page does with the answer happens in the browser and
nowhere else. Saving an edit sent the page to the URL it was already on, which
a browser treats as a fragment move rather than a load, so the write landed in
the store and the screen kept showing the old value.

So these drive a real Chromium at a real server over the DevTools protocol.
They are skipped where there is no browser to drive, which includes CI; the
suite must not need one.
"""

import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from ai_crontab import core

websockets_sync = pytest.importorskip("websockets.sync.client",
                                      reason="the DevTools client needs websockets")

CHROMIUM = next(
    (path for path in map(shutil.which,
                          ["chromium", "chromium-browser", "google-chrome",
                           "google-chrome-stable", "chrome"])
     if path),
    None,
)

pytestmark = pytest.mark.skipif(CHROMIUM is None, reason="no chromium to drive")

SRC = str(Path(__file__).parent.parent / "src")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(predicate, what, timeout=20.0):
    """Poll until something comes true, and name it if it never does."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.1)
    raise AssertionError(f"timed out waiting for {what}")


class Page:
    """One Chromium tab, driven over the DevTools protocol.

    Only two operations are needed — go to a URL and evaluate an expression —
    because everything these tests assert is a question about the document the
    browser is showing.
    """

    def __init__(self, ws):
        self.ws = ws
        self.next_id = 0
        self.call("Page.enable")
        self.call("Runtime.enable")

    def call(self, method, **params):
        self.next_id += 1
        self.ws.send(json.dumps({"id": self.next_id, "method": method,
                                 "params": params}))
        while True:
            message = json.loads(self.ws.recv())
            if message.get("id") == self.next_id:
                if "error" in message:
                    raise AssertionError(f"{method}: {message['error']}")
                return message.get("result")

    def evaluate(self, expression):
        result = self.call("Runtime.evaluate", expression=expression,
                           returnByValue=True, awaitPromise=True)
        if result.get("exceptionDetails"):
            raise AssertionError(f"{expression}: {result['exceptionDetails']}")
        return result["result"].get("value")

    def visit(self, url):
        self.call("Page.navigate", url=url)
        self.settle()

    def settle(self):
        wait_until(lambda: self.evaluate("document.readyState") == "complete",
                   "the page to finish loading")

    def stamp(self):
        """Mark this document, so a reload can be told from a fragment move.

        A page that reloaded has a new document and no mark. This is the whole
        difference the bug turned on, and it is invisible in the URL: both
        outcomes leave the same address in the bar.
        """
        self.evaluate("window.__stamp = 1")

    def stamp_survived(self):
        return self.evaluate("window.__stamp === 1")


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    """A headless browser for the whole module: launching one costs seconds."""
    debug_port = free_port()
    browser = subprocess.Popen(
        [CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu",
         f"--remote-debugging-port={debug_port}",
         f"--user-data-dir={tmp_path_factory.mktemp('chrome')}",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        targets = []

        def listed():
            nonlocal targets
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{debug_port}/json/list", timeout=1) as reply:
                    targets = [t for t in json.load(reply) if t["type"] == "page"]
            except (urllib.error.URLError, OSError, json.JSONDecodeError):
                return False
            return bool(targets)

        wait_until(listed, "chromium to open its debugging port")
        with websockets_sync.connect(targets[0]["webSocketDebuggerUrl"],
                                     max_size=None) as ws:
            yield Page(ws)
    finally:
        browser.terminate()
        browser.wait(timeout=10)


@pytest.fixture
def served(cfg):
    """A real `ai-crontab web`, because TestClient serves no JavaScript."""
    port = free_port()
    server = subprocess.Popen(
        [sys.executable, "-m", "ai_crontab", "--root", str(cfg.root),
         "web", "--port", str(port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        env={"PYTHONPATH": SRC, "PATH": "/usr/bin:/bin", "HOME": str(cfg.root)},
    )
    base = f"http://127.0.0.1:{port}"

    def answering():
        try:
            with urllib.request.urlopen(base + "/", timeout=1) as reply:
                return reply.status == 200
        except (urllib.error.URLError, OSError):
            return False

    try:
        wait_until(answering, "the web interface to come up")
        yield base
    finally:
        server.terminate()
        server.wait(timeout=10)


def test_saving_an_edit_shows_the_saved_task(page, served, store, make_task):
    """The regression: a write that redirects to the page it was made from.

    Every field of this is load-bearing. The store changing proves the route
    ran; the stamp being gone proves the page reloaded rather than only moving
    its fragment; and the rendered prompt is what the reader was actually
    looking for. Before the fix the first held and the other two did not.
    """
    task = make_task(name="probe")
    page.visit(f"{served}/tasks/{task.id}")
    assert "quiet" in page.evaluate("document.querySelector('pre.prompt').textContent")

    page.stamp()
    page.evaluate("""(() => {
      const form = document.querySelector('details.form form');
      form.querySelector('textarea[name=prompt]').value = 'edited in the browser';
      form.querySelector('button[type=submit]').click();
    })()""")
    wait_until(lambda: not page.stamp_survived(), "the page to reload", 10)
    page.settle()

    assert store.get_task(task.id).prompt == "edited in the browser"
    assert page.evaluate(
        "document.querySelector('pre.prompt').textContent") == "edited in the browser"
    assert page.evaluate("document.getElementById('flash').textContent") == "saved"


def test_a_second_save_reacts_like_the_first(page, served, store, make_task):
    """The URL is now byte-identical to the last one, which fires nothing.

    Assigning a fragment that is already there is not even a fragment move, so
    this is the case that stayed broken after any fix built on `hashchange`.
    """
    task = make_task(name="twice")
    page.visit(f"{served}/tasks/{task.id}")

    for text in ["first edit", "second edit"]:
        page.stamp()
        page.evaluate(f"""(() => {{
          const form = document.querySelector('details.form form');
          form.querySelector('textarea[name=prompt]').value = {json.dumps(text)};
          form.querySelector('button[type=submit]').click();
        }})()""")
        wait_until(lambda: not page.stamp_survived(), f"the reload after {text!r}", 10)
        page.settle()
        assert page.evaluate(
            "document.querySelector('pre.prompt').textContent") == text


def test_clearing_the_inbox_empties_it_on_screen(page, served, store, make_task):
    """The other page whose writes all redirect to themselves."""
    task = make_task(name="noisy")
    core.notify(store, task.id, None, "something happened")
    page.visit(f"{served}/inbox")
    assert "something happened" in page.evaluate("document.body.textContent")

    page.stamp()
    page.evaluate("document.querySelector('form[action=\"/inbox\"] button').click()")
    wait_until(lambda: not page.stamp_survived(), "the page to reload", 10)
    page.settle()

    assert "something happened" not in page.evaluate("document.body.textContent")
    assert "1 marked read" in page.evaluate(
        "document.getElementById('flash').textContent")


def test_deleting_one_notification_removes_it(page, served, store, make_task):
    """A form whose field is named `action` shadows `form.action`.

    The per-note forms post an `action` field, so in a browser `form.action`
    is that input rather than the URL, and posting to its string form reached
    a path nothing serves. The route was never called, so no route test could
    see it; only a browser resolves `form.action`. The confirm dialog is
    answered from here because a headless tab would otherwise wait on it.
    """
    task = make_task(name="noisy")
    keep = core.notify(store, task.id, None, "keep this one")
    drop = core.notify(store, task.id, None, "drop this one")
    page.visit(f"{served}/inbox")

    page.stamp()
    page.evaluate(f"""(() => {{
      window.confirm = () => true;
      document.querySelector(
        'form[action="/inbox/{drop}"] input[value=delete]').form
        .querySelector('button').click();
    }})()""")
    wait_until(lambda: not page.stamp_survived(), "the page to reload", 10)
    page.settle()

    assert store.get_notification(drop) is None
    assert store.get_notification(keep) is not None
    body = page.evaluate("document.body.textContent")
    assert "drop this one" not in body
    assert "keep this one" in body
    assert page.evaluate("document.getElementById('flash').textContent") == "deleted"


def test_marking_one_notification_read_clears_it(page, served, store, make_task):
    """The other per-note button, built on the same shadowed form."""
    task = make_task(name="noisy")
    note = core.notify(store, task.id, None, "read me")
    page.visit(f"{served}/inbox")

    page.stamp()
    page.evaluate(f"""document.querySelector(
      'form[action="/inbox/{note}"] input[value=read]').form
      .querySelector('button').click()""")
    wait_until(lambda: not page.stamp_survived(), "the page to reload", 10)
    page.settle()

    assert not store.get_notification(note).is_unread
    assert "read me" not in page.evaluate("document.body.textContent")
    assert page.evaluate("document.getElementById('flash').textContent") == "marked read"
