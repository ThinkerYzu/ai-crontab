"""Turn a run's stream-json transcript into rows a template can print.

The scheduler writes every line `claude` emits to
`tasks/<task-id>/runs/<run-id>.jsonl` and never reads it back. This module is
the first thing that does, which is the whole reason the web interface is worth
building: the file holds what an agent actually did overnight, and reading it
by hand means paging through JSON.

Nothing here may raise on a malformed file. A run killed at its time limit
leaves a truncated last line, and that run is exactly the one someone is trying
to read.
"""

import json
from dataclasses import dataclass, field

# Blocks the reader keeps, in the order `claude` emits them within a turn.
# Anything else in a message is passed through as an "other" row rather than
# dropped, so a new block type shows up as unfamiliar instead of invisible.
KNOWN_BLOCKS = ("thinking", "text", "tool_use", "tool_result")


@dataclass
class Row:
    """One thing that happened, ready to render.

    `kind` is what to draw: "thinking", "text", "tool_use", "tool_result",
    "other". `title` is the one-line summary a collapsed row shows, `body` the
    text behind it. `is_error` marks a tool result the agent was handed as a
    failure, which is worth colouring because it often explains what the run
    did next.
    """

    kind: str
    title: str
    body: str = ""
    is_error: bool = False


@dataclass
class Transcript:
    """A whole run, as rows plus what the `result` event said about it.

    `truncated` means the file's last line was incomplete, which is the normal
    state of a run killed at its limit rather than a corruption to report.
    `cost_usd`, `num_turns` and `duration_ms` come from the `result` event and
    are None when there is not one, which is itself the signal that the run did
    not finish on its own. `usage` is that event's token counts, worth showing
    because the cache read is what drives the bill, not the task prompt.
    """

    rows: list = field(default_factory=list)
    model: str | None = None
    cwd: str | None = None
    cost_usd: float | None = None
    num_turns: int | None = None
    duration_ms: int | None = None
    usage: dict = field(default_factory=dict)
    result_text: str = ""
    is_error: bool = False
    truncated: bool = False

    @property
    def finished(self):
        return self.num_turns is not None


def read(path, max_body=4000):
    """Parse a transcript file into a Transcript, tolerating any damage in it.

    `max_body` truncates each row's body, because a single tool result can hold
    a whole web page and a page of them would be unreadable anyway. A run with
    no transcript at all (killed before `claude` said anything) returns an
    empty Transcript rather than raising, so the run page still renders.
    """
    out = Transcript()
    try:
        raw = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return out

    for line in raw.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            # Only a final partial line is expected. Anything earlier is a
            # damaged file, and either way the rest of the run is still worth
            # showing, so record the fact and keep going.
            out.truncated = True
            continue
        if isinstance(event, dict):
            _add_event(out, event, max_body)
    return out


def _add_event(out, event, max_body):
    """Fold one stream-json event into the transcript being built."""
    kind = event.get("type")
    if kind == "system" and event.get("subtype") == "init":
        out.cwd = event.get("cwd")
        return
    if kind == "result":
        _add_result(out, event, max_body)
        return
    if kind in ("assistant", "user"):
        message = event.get("message") or {}
        out.model = message.get("model") or out.model
        for block in _blocks(message):
            row = _row(block, max_body)
            if row is not None:
                out.rows.append(row)


def _add_result(out, event, max_body):
    """Record what the final `result` event says the run cost and returned."""
    out.cost_usd = event.get("total_cost_usd")
    out.num_turns = event.get("num_turns")
    out.duration_ms = event.get("duration_ms")
    out.is_error = bool(event.get("is_error"))
    out.result_text = _clip(_text_of(event.get("result")), max_body)
    usage = event.get("usage")
    if isinstance(usage, dict):
        details = usage.get("output_tokens_details") or {}
        out.usage = {
            "input": usage.get("input_tokens"),
            "cache_write": usage.get("cache_creation_input_tokens"),
            "cache_read": usage.get("cache_read_input_tokens"),
            "output": usage.get("output_tokens"),
            "thinking": details.get("thinking_tokens"),
        }


def _blocks(message):
    """Yield a message's content blocks, whatever shape the content came in.

    Content is a list of blocks for everything the agent says, but a plain
    string in some events, and absent in others.
    """
    content = message.get("content")
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    if isinstance(content, str) and content.strip():
        return [{"type": "text", "text": content}]
    return []


def _row(block, max_body):
    """Turn one content block into a Row, or None if it holds nothing to show."""
    kind = block.get("type")
    if kind == "thinking":
        # Thinking arrives with a signature and an empty body: the text is not
        # in the transcript to show. Dropping the row keeps the page honest
        # rather than printing a run of empty "thinking" entries. That a run
        # thought at all is visible in the header's thinking-token count.
        body = _clip(block.get("thinking", ""), max_body)
        return Row("thinking", "thinking", body) if body.strip() else None
    if kind == "text":
        body = _clip(block.get("text", ""), max_body)
        return Row("text", "said", body) if body.strip() else None
    if kind == "tool_use":
        name = block.get("name", "tool")
        return Row("tool_use", name, _clip(_pretty(block.get("input")), max_body))
    if kind == "tool_result":
        body = _clip(_text_of(block.get("content")), max_body)
        return Row("tool_result", "result", body, bool(block.get("is_error")))
    return Row("other", str(kind), _clip(_pretty(block), max_body))


def _text_of(content):
    """Flatten a tool result's content, which is a string or a list of blocks.

    A search tool returns a list of typed blocks where a shell command returns
    a string, and a reader that assumed either one would show nothing for half
    the tools in a run.
    """
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(block.get("text") or _pretty(block))
        return "\n".join(p for p in parts if p)
    return _pretty(content)


def _pretty(value):
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, indent=1, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _clip(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "\n... (clipped)"
