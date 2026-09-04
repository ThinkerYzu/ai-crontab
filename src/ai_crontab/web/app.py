"""The web interface: routing, templates, and the checks that guard the port.

A fourth front end over `core`, adding no rule of its own. Every handler reads
the store or calls an operation the CLI already calls, so behaviour cannot
drift between the two.

Read `guard` before adding a route. The interface listens on loopback and has
no login, which is safe only for as long as the guard holds.
"""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import routes

_PKG_DIR = Path(__file__).resolve().parent

# Hosts a request may claim to be addressed to. Anything else is either a
# request that reached us through another name (which loopback-only binding
# should have made impossible) or a DNS rebinding attempt, and neither is a
# request this interface should answer.
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]", "::1"})


def make_app(cfg=None, root=None):
    """Build the application, without binding a port.

    `cfg` is a starting config, mostly for tests; a served app passes `root`
    instead and re-reads the config on every request, because a server left up
    for a week must notice an edit to `config.toml`
    ([Decision 13](DESIGN.md)). Nothing here opens the store: each request
    opens and closes its own, which is what keeps a long-lived reader from
    holding a connection across a run's writes.
    """
    app = FastAPI(title="ai-crontab", docs_url=None, redoc_url=None)
    app.state.config_root = root if root is not None else (cfg.root if cfg else None)
    app.state.templates = Jinja2Templates(directory=str(_PKG_DIR / "templates"))
    app.mount("/static", StaticFiles(directory=str(_PKG_DIR / "static")), name="static")
    app.middleware("http")(guard)
    app.include_router(routes.router)
    return app


async def guard(request, call_next):
    """Refuse any request that did not come from this machine's own interface.

    This is the whole security boundary, so it runs before routing rather than
    per-route: a route added later is covered by default instead of by
    remembering.

    Two separate things are checked. The `Host` header must name loopback,
    which stops a request that reached us under another name, DNS rebinding
    included. And a request that changes anything must also carry
    `X-AI-Crontab` and, if it declares an `Origin` at all, one of our own: a
    page on some other site can make your browser post here, and a task is a
    prompt that runs with every tool pre-granted, so that post would be
    arbitrary code execution. A login would not help, because the browser would
    attach the cookie to it. A header outside the CORS safelist would need a
    preflight this server never approves, so the request is never sent.
    """
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    if host not in LOOPBACK_HOSTS:
        return PlainTextResponse("ai-crontab serves loopback only\n", status_code=403)

    if request.method not in ("GET", "HEAD", "OPTIONS"):
        if request.headers.get("x-ai-crontab") is None:
            return PlainTextResponse("missing X-AI-Crontab header\n", status_code=403)
        origin = request.headers.get("origin")
        if origin is not None and _origin_host(origin) not in LOOPBACK_HOSTS:
            return PlainTextResponse("cross-site request refused\n", status_code=403)
    return await call_next(request)


def _origin_host(origin):
    """The hostname an `Origin` header names, with its scheme and port removed.

    Returns the header unchanged when it does not parse as a URL, so that
    something unrecognisable fails the loopback check instead of passing it.
    """
    host = origin.split("://", 1)[-1]
    host = host.split("/", 1)[0]
    return host.rsplit(":", 1)[0] if ":" in host and not host.endswith("]") else host


def serve(cfg):
    """Run the interface until interrupted, on loopback and nowhere else.

    The host is passed explicitly rather than left to a default, because the
    default is the one thing here that must never change silently.
    """
    import uvicorn

    app = make_app(root=cfg.root)
    uvicorn.run(app, host="127.0.0.1", port=cfg.web_port, log_level="warning")
