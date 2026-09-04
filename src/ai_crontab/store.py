"""Every SQLite query, and the transaction that makes a run a lock."""

import secrets
import sqlite3

from . import model
from .model import from_iso, to_iso

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id             TEXT PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE,
    prompt         TEXT NOT NULL,
    schedule_kind  TEXT NOT NULL,
    schedule       TEXT NOT NULL,
    working_dir    TEXT NOT NULL,
    time_limit_sec INTEGER NOT NULL,
    enabled        INTEGER NOT NULL DEFAULT 1,
    next_run       TEXT,
    created_by     TEXT NOT NULL DEFAULT 'user',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runs (
    id             TEXT PRIMARY KEY,
    task_id        TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    state          TEXT NOT NULL,
    supervisor_pid INTEGER,
    started_at     TEXT NOT NULL,
    ended_at       TEXT,
    exit_code      INTEGER,
    num_turns      INTEGER,
    cost_usd       REAL,
    summary        TEXT,
    error          TEXT
);
CREATE INDEX IF NOT EXISTS runs_by_task ON runs(task_id, started_at);
CREATE INDEX IF NOT EXISTS runs_by_start ON runs(started_at);

-- At most one active run per task. This index is the lock: two ticks racing
-- to start the same task cannot both insert, whatever their timing.
CREATE UNIQUE INDEX IF NOT EXISTS runs_one_active_per_task
    ON runs(task_id) WHERE state = 'active';

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id    TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    run_id     TEXT,
    priority   TEXT NOT NULL DEFAULT 'normal',
    title      TEXT NOT NULL,
    body       TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    read_at    TEXT
);
CREATE INDEX IF NOT EXISTS notifications_unread ON notifications(read_at, created_at);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def new_id(prefix, nbytes=3):
    return f"{prefix}_{secrets.token_hex(nbytes)}"


class Store:
    """The database, opened once per process.

    Writers are serialised by SQLite itself. WAL keeps the tick's reads from
    blocking behind an agent's writes, and the busy timeout absorbs the
    contention that remains when several runs call back at once.
    """

    def __init__(self, path):
        self.conn = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=10000")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self.set_meta("schema_version", str(SCHEMA_VERSION))

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- meta ------------------------------------------------------------

    def get_meta(self, key, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key, value):
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )

    # -- tasks -----------------------------------------------------------

    def add_task(self, task):
        self.conn.execute(
            "INSERT INTO tasks(id, name, prompt, schedule_kind, schedule, working_dir,"
            " time_limit_sec, enabled, next_run, created_by, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (task.id, task.name, task.prompt, task.schedule_kind, task.schedule,
             task.working_dir, task.time_limit_sec, int(task.enabled),
             to_iso(task.next_run), task.created_by,
             to_iso(task.created_at), to_iso(task.updated_at)),
        )
        return task

    def update_task(self, task):
        self.conn.execute(
            "UPDATE tasks SET name=?, prompt=?, schedule_kind=?, schedule=?,"
            " working_dir=?, time_limit_sec=?, enabled=?, next_run=?, updated_at=?"
            " WHERE id=?",
            (task.name, task.prompt, task.schedule_kind, task.schedule,
             task.working_dir, task.time_limit_sec, int(task.enabled),
             to_iso(task.next_run), to_iso(task.updated_at), task.id),
        )

    def remove_task(self, task_id):
        cur = self.conn.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        return cur.rowcount > 0

    def get_task(self, task_id):
        row = self.conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return _task(row)

    def find_task(self, ref):
        """Look a task up by id or by name, in that order.

        The CLI and the agent both name tasks the way a person would, so every
        command that takes a task accepts either.
        """
        return self.get_task(ref) or _task(
            self.conn.execute("SELECT * FROM tasks WHERE name=?", (ref,)).fetchone()
        )

    def list_tasks(self):
        rows = self.conn.execute("SELECT * FROM tasks ORDER BY name").fetchall()
        return [_task(r) for r in rows]

    def due_tasks(self, now):
        """Enabled tasks whose time has come and that are not already running.

        Ordered most overdue first, which is what keeps a frequent task from
        crowding out a daily one when the concurrency ceiling is reached.
        """
        rows = self.conn.execute(
            "SELECT t.* FROM tasks t"
            " WHERE t.enabled=1 AND t.next_run IS NOT NULL AND t.next_run <= ?"
            "   AND NOT EXISTS (SELECT 1 FROM runs r"
            "                   WHERE r.task_id=t.id AND r.state='active')"
            " ORDER BY t.next_run",
            (to_iso(now),),
        ).fetchall()
        return [_task(r) for r in rows]

    # -- runs ------------------------------------------------------------

    def claim_run(self, task_id, now):
        """Open an active run for a task, or return None if one is open already.

        The unique partial index does the deciding, so two ticks racing on the
        same task cannot both succeed however their transactions interleave.
        The caller only starts a supervisor when this returns an id.
        """
        run_id = new_id("r", 4)
        try:
            self.conn.execute(
                "INSERT INTO runs(id, task_id, state, started_at) VALUES(?,?,?,?)",
                (run_id, task_id, model.ACTIVE, to_iso(now)),
            )
        except sqlite3.IntegrityError:
            return None
        return run_id

    def adopt_run(self, run_id, pid):
        """Record which process is supervising a run.

        Until this lands the run has no pid, and reaping treats it as still
        starting up rather than dead.
        """
        self.conn.execute("UPDATE runs SET supervisor_pid=? WHERE id=?", (pid, run_id))

    def finish_run(self, run_id, state, now, exit_code=None, summary=None,
                   error=None, num_turns=None, cost_usd=None):
        self.conn.execute(
            "UPDATE runs SET state=?, ended_at=?, exit_code=?, summary=?, error=?,"
            " num_turns=?, cost_usd=? WHERE id=?",
            (state, to_iso(now), exit_code, summary, error, num_turns, cost_usd, run_id),
        )

    def get_run(self, run_id):
        row = self.conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        return _run(row)

    def active_runs(self):
        rows = self.conn.execute(
            "SELECT * FROM runs WHERE state='active' ORDER BY started_at"
        ).fetchall()
        return [_run(r) for r in rows]

    def count_active_runs(self):
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM runs WHERE state='active'"
        ).fetchone()["n"]

    def run_history(self, task_id, limit=20):
        rows = self.conn.execute(
            "SELECT * FROM runs WHERE task_id=? ORDER BY started_at DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [_run(r) for r in rows]

    # -- notifications ---------------------------------------------------

    def add_notification(self, task_id, run_id, priority, title, body, now):
        cur = self.conn.execute(
            "INSERT INTO notifications(task_id, run_id, priority, title, body, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (task_id, run_id, priority, title, body, to_iso(now)),
        )
        return cur.lastrowid

    def list_notifications(self, unread_only=True, limit=50):
        """Newest first, high priority before low within the same read state."""
        sql = ("SELECT * FROM notifications"
               + (" WHERE read_at IS NULL" if unread_only else "")
               + " ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'normal' THEN 1"
                 " ELSE 2 END, created_at DESC LIMIT ?")
        return [_notification(r) for r in self.conn.execute(sql, (limit,)).fetchall()]

    def get_notification(self, notification_id):
        row = self.conn.execute(
            "SELECT * FROM notifications WHERE id=?", (notification_id,)
        ).fetchone()
        return _notification(row)

    def mark_read(self, notification_id, now):
        cur = self.conn.execute(
            "UPDATE notifications SET read_at=? WHERE id=? AND read_at IS NULL",
            (to_iso(now), notification_id),
        )
        return cur.rowcount > 0

    def mark_all_read(self, now):
        cur = self.conn.execute(
            "UPDATE notifications SET read_at=? WHERE read_at IS NULL", (to_iso(now),)
        )
        return cur.rowcount

    def delete_notification(self, notification_id):
        cur = self.conn.execute("DELETE FROM notifications WHERE id=?", (notification_id,))
        return cur.rowcount > 0

    def unread_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM notifications WHERE read_at IS NULL"
        ).fetchone()["n"]

    # -- pruning ---------------------------------------------------------

    def prune(self, cutoff):
        """Delete read notifications and finished runs older than the cutoff.

        Returns the run ids that went, so the caller can remove their
        transcripts. Unread notifications and active runs are never pruned: an
        unread notification is the one thing the user has not seen yet, and an
        active run is not history.
        """
        gone = [r["id"] for r in self.conn.execute(
            "SELECT id FROM runs WHERE state != 'active' AND started_at < ?",
            (to_iso(cutoff),),
        ).fetchall()]
        self.conn.execute(
            "DELETE FROM runs WHERE state != 'active' AND started_at < ?", (to_iso(cutoff),)
        )
        self.conn.execute(
            "DELETE FROM notifications WHERE read_at IS NOT NULL AND created_at < ?",
            (to_iso(cutoff),),
        )
        return gone


def _task(row):
    if row is None:
        return None
    return model.Task(
        id=row["id"], name=row["name"], prompt=row["prompt"],
        schedule_kind=row["schedule_kind"], schedule=row["schedule"],
        working_dir=row["working_dir"], time_limit_sec=row["time_limit_sec"],
        enabled=bool(row["enabled"]), next_run=from_iso(row["next_run"]),
        created_by=row["created_by"], created_at=from_iso(row["created_at"]),
        updated_at=from_iso(row["updated_at"]),
    )


def _run(row):
    if row is None:
        return None
    return model.Run(
        id=row["id"], task_id=row["task_id"], state=row["state"],
        supervisor_pid=row["supervisor_pid"], started_at=from_iso(row["started_at"]),
        ended_at=from_iso(row["ended_at"]), exit_code=row["exit_code"],
        num_turns=row["num_turns"], cost_usd=row["cost_usd"],
        summary=row["summary"], error=row["error"],
    )


def _notification(row):
    if row is None:
        return None
    return model.Notification(
        id=row["id"], task_id=row["task_id"], run_id=row["run_id"],
        priority=row["priority"], title=row["title"], body=row["body"],
        created_at=from_iso(row["created_at"]), read_at=from_iso(row["read_at"]),
    )
