"""A local browser UI in front of the same commands the terminal runs.

The CLI is the product; this is a front door for people who will not open a
terminal. Three rules keep it from becoming a second, disagreeing product:

  * **No scoring, no source selection, no output contract lives here.** Every
    request is answered by running `cli.main([...,'--json'])` in-process and
    handing back that exact document. The page can render the numbers, never
    compute them, so it cannot drift from `dotameta ... --json`.
  * **Query parameters are translated, never forwarded.** `PARAMS` is an
    allowlist of name -> validator; a value becomes a flag only after it passes.
    Nothing a browser sends can turn into an arbitrary argv entry.
  * **The server is loopback-only.** It binds `127.0.0.1` by default and rejects
    requests whose `Host` header is not a loopback name, so a hostile page in the
    same browser cannot use the API through DNS rebinding. Credentials stay in
    the environment and are never sent to the page.

`--json` owns stdout for one document at a time, so requests are serialised on
`_CLI_LOCK` while stdout is redirected.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import cli
from ._version import __version__
from .config import Settings

# Serialises the redirected-stdout window below. A local UI has one user, so a
# lock is honest and cheap; without it two tabs would interleave one JSON body.
_CLI_LOCK = threading.Lock()

ROLE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z ]{0,19}$")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}

# The page is inline HTML, CSS and JS, and reaches exactly two outside hosts:
# Valve's CDN for hero portraits and OpenDota's constants endpoint that maps a
# hero id to one. Everything else, including any script source, is refused.
FAVICON = (
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 256 256'>"
    "<rect width='256' height='256' rx='56' fill='#1f1c1b'/>"
    "<rect x='46' y='168' width='42' height='42' rx='9' fill='#7a6f66'/>"
    "<rect x='104' y='140' width='42' height='70' rx='9' fill='#7a6f66'/>"
    "<polygon points='183,46 232,106 134,106' fill='#61d69a'/>"
    "<rect x='162' y='100' width='42' height='110' rx='9' fill='#61d69a'/>"
    "</svg>"
)

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; "
    "img-src 'self' data: https://cdn.cloudflare.steamstatic.com; "
    "connect-src 'self' https://api.opendota.com; "
    "style-src 'unsafe-inline'; "
    "script-src 'unsafe-inline'; "
    "base-uri 'none'; "
    "form-action 'none'"
)


class UiError(Exception):
    """A bad request from the page: reported as JSON, never as a traceback."""


def _bounded_int(name: str, low: int, high: int) -> Callable[[str], list[str]]:
    def convert(raw: str) -> list[str]:
        try:
            number = int(raw)
        except ValueError:
            raise UiError(f"{name} must be a whole number, got {raw!r}") from None
        if not low <= number <= high:
            raise UiError(f"{name} must be between {low} and {high}, got {number}")
        return [f"--{name}", str(number)]

    return convert


def _account(raw: str) -> list[str]:
    # Reuse the CLI parser so the page accepts exactly the ids and profile URLs
    # the terminal accepts, and rejects the same junk.
    try:
        cli.parse_account_id(raw)
    except Exception as error:  # argparse raises ArgumentTypeError
        raise UiError(str(error)) from None
    return ["--account-id", raw]


def _role(raw: str) -> list[str]:
    if not ROLE_PATTERN.match(raw):
        raise UiError(f"role must be a plain tag such as Carry or Support, got {raw!r}")
    return ["--role", raw]


def _source(raw: str) -> list[str]:
    if raw not in ("auto", "opendota", "stratz"):
        raise UiError(f"source must be auto, opendota, or stratz, got {raw!r}")
    return ["--source", raw]


def _flag(name: str) -> Callable[[str], list[str]]:
    def convert(raw: str) -> list[str]:
        return [f"--{name}"] if raw in ("1", "true", "on", "yes") else []

    return convert


PARAMS: dict[str, Callable[[str], list[str]]] = {
    "account-id": _account,
    "bracket": _bounded_int("bracket", 1, 8),
    "position": _bounded_int("position", 1, 5),
    "role": _role,
    "source": _source,
    "days": _bounded_int("days", 0, 3650),
    "top": _bounded_int("top", 1, 200),
    "pool": _bounded_int("pool", 1, 20),
    "min-picks": _bounded_int("min-picks", 0, 10_000_000),
    "min-games": _bounded_int("min-games", 0, 100_000),
    "played-only": _flag("played-only"),
}

# Which allowlisted parameters each command actually understands. Sending a
# parameter the subcommand has no flag for is a request error, not a silent drop.
COMMAND_PARAMS = {
    "recommend": set(PARAMS),
    "meta": {"bracket", "position", "role", "source", "top"},
    "player": {"account-id", "days", "source"},
}


def build_argv(command: str, query: dict[str, list[str]]) -> list[str]:
    """Turn validated query parameters into the argv a terminal user would type."""
    allowed = COMMAND_PARAMS.get(command)
    if allowed is None:
        raise UiError(f"unknown command {command!r}")
    argv = [command]
    for name, values in query.items():
        if name not in PARAMS:
            raise UiError(f"unknown parameter {name!r}")
        if name not in allowed:
            raise UiError(f"{command} does not accept {name!r}")
        if len(values) != 1:
            raise UiError(f"{name} was given {len(values)} times")
        value = values[0].strip()
        if not value:
            continue
        argv.extend(PARAMS[name](value))
    argv.append("--json")
    return argv


def run_json(argv: list[str]) -> tuple[int, dict[str, Any]]:
    """Run one CLI command and return its exit code with a JSON body.

    A nonzero exit leaves stdout empty by contract, so the diagnostic the CLI
    wrote to stderr becomes the error message the page shows.
    """
    out, err = io.StringIO(), io.StringIO()
    with _CLI_LOCK:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(argv)
            except SystemExit as exit_error:  # argparse refused a value
                code = int(exit_error.code or 0)
            except Exception as error:  # never take the server down with a request
                code = 2
                err.write(f"{type(error).__name__}: {error}")
    if code == 0:
        try:
            return 0, json.loads(out.getvalue())
        except json.JSONDecodeError:
            return 2, {"error": "the command produced no JSON document"}
    message = err.getvalue().strip().splitlines()
    return code, {"error": message[-1] if message else f"command failed with code {code}"}


def _host_is_local(header: str | None, port: int) -> bool:
    if not header:
        return False
    name = header.rsplit(":", 1)[0] if header.count(":") == 1 else header
    if header.startswith("["):  # [::1]:8765
        name = header.split("]")[0] + "]"
    return name in LOOPBACK_HOSTS


def render_page() -> str:
    """The page, with the running version, account and Stratz availability filled in.

    `DOTAMETA_ACCOUNT_ID` is documented as a convenience default rather than a
    credential, and the CLI already applies it. Showing it means an empty field
    really does mean "no account", which is what lets the form require one.
    Stratz is reported as a yes or no so the page can disable a control that
    would otherwise fail every time; the token itself never reaches the page.
    """
    settings = Settings.from_env()
    account = settings.account_id
    return (
        PAGE.replace("{{version}}", __version__)
        .replace("{{account}}", str(account) if account else "")
        # Whether a token exists, never the token: the page uses it to stop
        # offering position meta that is known to fail.
        .replace("{{stratz}}", "1" if settings.has_stratz else "")
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "dotameta"
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
        # The terminal running the UI should stay readable; failures still
        # surface in the page itself.
        pass

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # Nothing here is meant for another origin or another page's frame.
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CONTENT_SECURITY_POLICY)
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        self._send(code, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:  # noqa: N802 - stdlib signature
        if not _host_is_local(self.headers.get("Host"), self.server.server_address[1]):
            self._send_json(403, {"error": "this UI only answers loopback requests"})
            return
        parsed = urlparse(self.path)
        if parsed.path in ("/", "/index.html"):
            self._send(200, render_page().encode("utf-8"), "text/html; charset=utf-8")
            return
        if parsed.path == "/icon.svg":
            self._send(200, FAVICON.encode("utf-8"), "image/svg+xml")
            return
        if parsed.path == "/favicon.ico":
            # The page carries its own inline icon; answering with a JSON 404
            # only puts a red line in the user's console.
            self._send(204, b"", "image/x-icon")
            return
        if parsed.path.startswith("/api/"):
            command = parsed.path[len("/api/") :]
            try:
                argv = build_argv(command, parse_qs(parsed.query, keep_blank_values=False))
            except UiError as error:
                self._send_json(400, {"error": str(error)})
                return
            code, payload = run_json(argv)
            self._send_json(200 if code == 0 else 502, payload)
            return
        self._send_json(404, {"error": "not found"})


def serve(host: str, port: int, open_browser: bool = True) -> ThreadingHTTPServer:
    """Bind the server and return it. The caller owns `serve_forever`."""
    server = ThreadingHTTPServer((host, port), Handler)
    if open_browser:
        url = f"http://{host}:{server.server_address[1]}/"
        # A failed browser launch must not stop a server that already works.
        with contextlib.suppress(Exception):
            webbrowser.open(url)
    return server


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>dotameta</title>
<link rel="icon" type="image/svg+xml" href="/icon.svg">
<style>
:root {
  color-scheme: dark;
  /* Warm neutrals carry the Dota feel; the semantic colours stay separate so a
     verdict never has to compete with brand chrome for meaning. */
  --bg: #0e0d0d;
  --panel: #171515;
  --raised: #1e1b1a;
  --line: #302b29;
  --line-soft: #262221;
  --text: #eeeae2;
  --dim: #b0a9a0;
  --faint: #888079;
  --brand-red: #b5433c;
  --brand-gold: #c9a96a;
  --accent: #61d69a;
  --accent-ink: #06130c;
  --spam: #61d69a;
  --keep: #8fc6a6;
  --risky: #e4bf62;
  --learn: #72c7df;
  --drop: #e57f83;
  --focus: #eeeae2;
  --radius: 12px;
}
* { box-sizing: border-box; }
/* A class with `display` beats the user agent rule for [hidden], and an
   empty flex panel still paints its border. Settle it once, here. */
[hidden] { display: none !important; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  background: radial-gradient(1000px 480px at 50% -170px, #b5433c1f, transparent), var(--bg);
  color: var(--text);
  font: 15px/1.55 "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, sans-serif;
}
.app { max-width: 1160px; margin: 0 auto; padding: 28px 24px 56px; }
.sr-only {
  position: absolute;
  width: 1px;
  height: 1px;
  padding: 0;
  margin: -1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
  border: 0;
}
:focus-visible { outline: 2px solid var(--focus); outline-offset: 3px; }

/* header */
.topbar { display: flex; align-items: center; gap: 14px; margin-bottom: 22px; }
.mark { width: 38px; height: 38px; flex: none; }
.topbar h1 { margin: 0; font-size: 19px; font-weight: 650; letter-spacing: .01em; }
.topbar p { margin: 2px 0 0; color: var(--dim); font-size: 13px; }
.langs { margin-left: auto; display: flex; gap: 2px; align-self: flex-start; }
.langs button {
  background: none;
  border: 1px solid transparent;
  border-radius: 7px;
  color: var(--faint);
  font-size: 12px;
  font-weight: 600;
  letter-spacing: .06em;
  padding: 5px 10px;
}
.langs button:hover:not(:disabled) { color: var(--text); filter: none; }
.langs button[aria-pressed="true"] {
  color: var(--brand-gold);
  border-color: var(--line);
  background: #ffffff08;
}

/* form */
.panel {
  background: linear-gradient(180deg, var(--raised), var(--panel));
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 18px;
  box-shadow: 0 1px 0 #ffffff08 inset, 0 8px 24px #00000055;
}
.fields {
  display: grid;
  gap: 14px;
  grid-template-columns: repeat(auto-fit, minmax(112px, 1fr));
}
.field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.field--wide { grid-column: span 2; min-width: 180px; }
.field > span {
  font-size: 11px;
  letter-spacing: .07em;
  text-transform: uppercase;
  color: var(--dim);
}
.field small { color: var(--faint); font-size: 11px; line-height: 1.35; }
input, select {
  width: 100%;
  background: #100e0e;
  color: var(--text);
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 9px 10px;
  font: inherit;
  font-size: 14px;
  transition: border-color .12s, box-shadow .12s;
}
input::placeholder { color: var(--faint); }
input:focus, select:focus { border-color: var(--brand-gold); }
select:disabled, option:disabled { color: var(--faint); }
.field.invalid select, .field.invalid input { border-color: var(--drop); }
.actions {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-top: 16px;
  flex-wrap: wrap;
}
.toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  color: var(--dim);
  cursor: pointer;
}
.toggle input {
  width: 16px;
  height: 16px;
  padding: 0;
  accent-color: var(--accent);
  cursor: pointer;
}
button {
  display: flex;
  align-items: center;
  gap: 9px;
  background: var(--accent);
  color: var(--accent-ink);
  border: 0;
  border-radius: 8px;
  padding: 10px 22px;
  font: inherit;
  font-weight: 650;
  cursor: pointer;
  transition: filter .12s, transform .06s;
}
#go { margin-left: auto; }
button:hover:not(:disabled) { filter: brightness(1.08); }
button:active:not(:disabled) { transform: translateY(1px); }
button:disabled { opacity: .6; cursor: progress; }
#go svg { width: 15px; height: 15px; }
.ghost {
  background: none;
  border: 1px solid var(--line);
  color: var(--text);
  font-weight: 600;
  font-size: 12.5px;
  padding: 7px 14px;
}
.ghost:hover:not(:disabled) { border-color: var(--brand-gold); filter: none; }

details.help { margin-top: 14px; }
details.help summary {
  cursor: pointer;
  color: var(--faint);
  font-size: 12px;
  list-style: none;
}
details.help summary::-webkit-details-marker { display: none; }
details.help summary::before { content: "+ "; }
details.help[open] summary::before { content: "- "; }
details.help summary:hover { color: var(--dim); }
details.help .body {
  margin-top: 8px;
  padding: 12px 14px;
  border: 1px solid var(--line-soft);
  border-radius: 10px;
  font-size: 12.5px;
  color: var(--dim);
  background: #ffffff04;
}
details.help ul { margin: 6px 0 0; padding-left: 18px; }
details.help li { margin: 3px 0; }
details.help b { color: var(--text); }

/* feedback */
.note {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  flex-wrap: wrap;
  margin: 16px 0 0;
  padding: 11px 14px;
  border-radius: 10px;
  font-size: 13px;
  color: var(--dim);
  background: #ffffff06;
  border: 1px solid var(--line-soft);
}
.note.error {
  color: #f2a7a9;
  background: #e57f8314;
  border-color: #e57f8340;
}
.note svg { width: 15px; height: 15px; flex: none; margin-top: 1px; }
.note .text { flex: 1; min-width: 220px; }
.note button { margin: -3px 0; }
.spinner {
  width: 14px;
  height: 14px;
  border-radius: 50%;
  flex: none;
  margin-top: 1px;
  border: 2px solid #ffffff22;
  border-top-color: var(--accent);
  animation: spin .7s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.empty {
  margin-top: 22px;
  padding: 42px 24px;
  text-align: center;
  border: 1px dashed var(--line);
  border-radius: var(--radius);
  color: var(--dim);
  font-size: 14px;
}
.empty svg { width: 30px; height: 30px; opacity: .5; display: block; margin: 0 auto 12px; }
.empty code {
  color: var(--faint);
  background: #ffffff0a;
  border-radius: 5px;
  padding: 1px 6px;
  font-size: 12.5px;
}
.stale {
  margin: 16px 0 0;
  padding: 8px 14px;
  border-radius: 10px;
  font-size: 12px;
  color: var(--brand-gold);
  background: #c9a96a14;
  border: 1px solid #c9a96a33;
}

/* results */
.stats {
  display: grid;
  gap: 12px;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  margin-top: 22px;
}
.stat {
  background: var(--panel);
  border: 1px solid var(--line-soft);
  border-radius: 10px;
  padding: 12px 14px;
}
.stat span {
  display: block;
  font-size: 10px;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: var(--faint);
}
.stat strong {
  display: block;
  margin-top: 4px;
  font-size: 17px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
}
.stat small { display: block; margin-top: 3px; color: var(--faint); font-size: 11px; }
.stat.headline strong { color: var(--accent); }
.stat a { color: var(--learn); font-size: 14px; }
.stat .was { color: var(--faint); font-weight: 400; }

.pool {
  margin-top: 12px;
  background: var(--panel);
  border: 1px solid var(--line-soft);
  border-radius: 10px;
  padding: 14px;
}
.pool h2 {
  margin: 0 0 12px;
  font-size: 11px;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: var(--faint);
  font-weight: 600;
}
.pool-heroes { display: flex; flex-wrap: wrap; gap: 10px; }
.hero-card {
  display: flex;
  align-items: center;
  gap: 10px;
  background: #61d69a12;
  border: 1px solid #61d69a33;
  border-radius: 10px;
  padding: 8px 14px 8px 8px;
}
.hero-card .art { width: 64px; height: 36px; }
.hero-card b { display: block; font-size: 13.5px; font-weight: 600; }
.hero-card small {
  display: block;
  color: var(--faint);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}
.pool-note { margin: 12px 0 0; font-size: 12px; color: var(--faint); }

.art {
  width: 44px;
  height: 25px;
  border-radius: 4px;
  object-fit: cover;
  background: #2a2523;
  flex: none;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  font-weight: 700;
  color: var(--faint);
  letter-spacing: .04em;
}

.warnings {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 12px 0 0;
  padding: 12px 14px;
  list-style: none;
  background: #e4bf620d;
  border: 1px solid #e4bf6233;
  border-radius: 10px;
}
.warnings li {
  display: flex;
  gap: 9px;
  align-items: flex-start;
  color: #dfbf78;
  font-size: 12.5px;
}
.warnings svg { width: 14px; height: 14px; flex: none; margin-top: 2px; }

.tablecard {
  margin-top: 12px;
  background: var(--panel);
  border: 1px solid var(--line-soft);
  border-radius: 10px;
  overflow: hidden;
}
.legend { padding: 12px 14px 0; }
.legend dl { margin: 6px 0 0; font-size: 12.5px; color: var(--dim); }
.legend dt {
  display: inline-block;
  min-width: 92px;
  font-weight: 700;
  text-transform: uppercase;
  font-size: 10.5px;
  letter-spacing: .06em;
}
.legend dd { display: inline; margin: 0; }
.legend div { margin: 5px 0; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; width: 100%; min-width: 780px; font-size: 14px; }
th {
  position: sticky;
  top: 0;
  background: var(--raised);
  text-align: left;
  padding: 0;
  border-bottom: 1px solid var(--line);
  white-space: nowrap;
}
th button {
  width: 100%;
  background: none;
  border: 0;
  border-radius: 0;
  color: var(--faint);
  font: inherit;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: .08em;
  text-transform: uppercase;
  padding: 10px 14px;
  gap: 5px;
  cursor: pointer;
}
th button:hover:not(:disabled) { color: var(--dim); filter: none; }
th.num button { justify-content: flex-end; }
th button .caret { color: var(--brand-gold); }
td {
  padding: 9px 14px;
  border-bottom: 1px solid var(--line-soft);
  white-space: nowrap;
  vertical-align: middle;
}
tr.group td {
  padding: 12px 14px 6px;
  border-bottom: 1px solid var(--line-soft);
  font-size: 10px;
  letter-spacing: .08em;
  text-transform: uppercase;
  color: var(--faint);
  background: #ffffff03;
}
tbody tr.row { cursor: pointer; transition: background .1s; }
tbody tr.row:hover { background: #ffffff06; }
tbody tr.row.open { background: #ffffff08; }
tbody tr.row.in-pool td:first-child { box-shadow: inset 3px 0 0 var(--accent); }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
td.hero { font-weight: 600; }
td.faint { color: var(--faint); }
.heroname { display: flex; align-items: center; gap: 10px; }
.chevron {
  background: none;
  border: 0;
  padding: 4px;
  margin-left: -4px;
  color: var(--faint);
  cursor: pointer;
  border-radius: 6px;
  transition: transform .12s, color .12s;
}
.chevron svg { width: 13px; height: 13px; display: block; }
.chevron:hover { color: var(--text); filter: none; }
tr.row.open .chevron { transform: rotate(90deg); color: var(--text); }
.up { color: var(--spam); }
.down { color: var(--drop); }
.pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  border-radius: 999px;
  padding: 3px 10px 3px 8px;
  font-size: 10.5px;
  font-weight: 700;
  letter-spacing: .06em;
  text-transform: uppercase;
}
.pill svg { width: 11px; height: 11px; }
.pill.spam { background: #61d69a1f; color: var(--spam); }
.pill.keep { background: #8fc6a61f; color: var(--keep); }
.pill.risky { background: #e4bf621f; color: var(--risky); }
.pill.learn { background: #72c7df1f; color: var(--learn); }
.pill.drop { background: #e57f831a; color: var(--drop); }
.bar {
  display: flex;
  justify-content: flex-end;
  height: 4px;
  border-radius: 2px;
  background: #ffffff10;
  margin-top: 5px;
  overflow: hidden;
}
.bar i { display: block; height: 100%; background: var(--accent); opacity: .75; }
tr.reasons td {
  white-space: normal;
  color: var(--dim);
  font-size: 12.5px;
  background: #ffffff04;
  padding-top: 0;
}
tr.reasons ul { margin: 0 0 6px; padding-left: 18px; }
tr.reasons li { margin: 3px 0; }
.tags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.tag {
  background: #ffffff0d;
  border: 1px solid var(--line-soft);
  border-radius: 999px;
  padding: 2px 9px;
  font-size: 11px;
  color: var(--faint);
}
.skeleton td { height: 41px; }
.skeleton i {
  display: block;
  height: 9px;
  border-radius: 4px;
  background: linear-gradient(90deg, #ffffff08, #ffffff14, #ffffff08);
  animation: pulse 1.2s ease-in-out infinite;
}
@keyframes pulse { 50% { opacity: .45; } }

footer {
  margin-top: 26px;
  color: var(--faint);
  font-size: 12px;
  line-height: 1.6;
  max-width: 800px;
}
footer .version { color: var(--faint); }

@media (max-width: 640px) {
  .app { padding: 20px 16px 40px; }
  .fields { grid-template-columns: 1fr; }
  .field--wide { grid-column: auto; }
  #go { width: 100%; margin-left: 0; justify-content: center; }
  .topbar p { font-size: 12.5px; }
}
@media (prefers-reduced-motion: reduce) {
  .spinner, .skeleton i { animation: none; }
  * { transition: none !important; }
}
</style>
</head>
<body data-stratz="{{stratz}}">
<div class="app">
  <div class="topbar">
    <svg class="mark" viewBox="0 0 256 256" aria-hidden="true">
      <rect x="1" y="1" width="254" height="254" rx="56"
            fill="#1f1c1b" stroke="#3a3330" stroke-width="3"/>
      <rect x="46" y="168" width="42" height="42" rx="9" fill="#7a6f66"/>
      <rect x="104" y="140" width="42" height="70" rx="9" fill="#7a6f66"/>
      <polygon points="183,46 232,106 134,106" fill="#61d69a"/>
      <rect x="162" y="100" width="42" height="110" rx="9" fill="#61d69a"/>
    </svg>
    <div>
      <h1>dotameta</h1>
      <p data-i18n="tagline"></p>
    </div>
    <div class="langs" role="group" data-i18n-aria="langGroup">
      <button type="button" id="lang-en" data-lang="en">EN</button>
      <button type="button" id="lang-ru" data-lang="ru">RU</button>
    </div>
  </div>

  <form class="panel" id="form">
    <div class="fields">
      <label class="field field--wide">
        <span data-i18n="account"></span>
        <input id="account" value="{{account}}" required data-i18n-ph="accountPlaceholder"
               autocomplete="off" spellcheck="false">
      </label>
      <label class="field" id="field-bracket">
        <span data-i18n="metaBracket"></span>
        <select id="bracket">
          <option value="" data-i18n="fromRank"></option>
          <option value="1" data-i18n="medal1"></option>
          <option value="2" data-i18n="medal2"></option>
          <option value="3" data-i18n="medal3"></option>
          <option value="4" data-i18n="medal4"></option>
          <option value="5" data-i18n="medal5"></option>
          <option value="6" data-i18n="medal6"></option>
          <option value="7" data-i18n="medal7"></option>
          <option value="8" data-i18n="medal8"></option>
        </select>
      </label>
      <label class="field">
        <span data-i18n="capabilityTag"></span>
        <select id="role">
          <option value="" data-i18n="any"></option>
          <option value="Carry" data-i18n="tagCarry"></option>
          <option value="Support" data-i18n="tagSupport"></option>
          <option value="Nuker" data-i18n="tagNuker"></option>
          <option value="Disabler" data-i18n="tagDisabler"></option>
          <option value="Initiator" data-i18n="tagInitiator"></option>
          <option value="Durable" data-i18n="tagDurable"></option>
          <option value="Escape" data-i18n="tagEscape"></option>
          <option value="Pusher" data-i18n="tagPusher"></option>
        </select>
        <small data-i18n="capabilityNote"></small>
      </label>
      <label class="field" id="field-position">
        <span data-i18n="metaPosition"></span>
        <select id="position">
          <option value="" data-i18n="any"></option>
          <option value="1" data-i18n="pos1"></option>
          <option value="2" data-i18n="pos2"></option>
          <option value="3" data-i18n="pos3"></option>
          <option value="4" data-i18n="pos4"></option>
          <option value="5" data-i18n="pos5"></option>
        </select>
        <small id="position-note" data-i18n="positionNote"></small>
      </label>
      <label class="field">
        <span data-i18n="personalHistory"></span>
        <select id="days">
          <option value="30" data-i18n="days30"></option>
          <option value="90" selected data-i18n="days90"></option>
          <option value="365" data-i18n="days365"></option>
          <option value="0" data-i18n="daysAll"></option>
        </select>
      </label>
      <label class="field">
        <span data-i18n="maxPoolSize"></span>
        <input id="pool" type="number" min="1" max="20" value="3">
        <small data-i18n="poolHint"></small>
      </label>
      <label class="field">
        <span data-i18n="tableRows"></span>
        <input id="top" type="number" min="1" max="200" value="15">
      </label>
    </div>
    <div class="actions">
      <label class="toggle">
        <input id="played" type="checkbox"><span data-i18n="hideUnplayed"></span>
      </label>
      <button id="go" type="submit">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"
             stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M3 17l6-6 4 4 7-7"/><path d="M14 8h6v6"/>
        </svg>
        <span data-i18n="recommend"></span>
      </button>
    </div>
    <details class="help" id="stratz-help">
      <summary data-i18n="stratzSummary"></summary>
      <div class="body">
        <span data-i18n="stratzBody"></span>
        <ul>
          <li data-i18n="stratzImmortal"></li>
          <li data-i18n="stratzPosition"></li>
        </ul>
      </div>
    </details>
  </form>

  <p class="note" id="note" aria-live="polite" aria-atomic="true" hidden></p>
  <p class="stale" id="stale" data-i18n="staleResults" hidden></p>

  <div class="empty" id="empty">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"
         stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <circle cx="11" cy="11" r="7"/><path d="M20 20l-4.2-4.2"/>
    </svg>
    <span data-i18n="emptyState"></span><br>
    <code>123456789</code> <span data-i18n="or"></span>
    <code>opendota.com/players/123456789</code>
  </div>

  <section id="result" hidden>
    <div class="stats" id="stats"></div>
    <div class="pool" id="pool-card" hidden>
      <h2 data-i18n="suggestedPool"></h2>
      <div class="pool-heroes" id="pool-heroes"></div>
      <p class="pool-note" id="pool-note"></p>
    </div>
    <ul class="warnings" id="warnings" hidden></ul>
    <div class="tablecard">
      <details class="help legend">
        <summary data-i18n="legendSummary"></summary>
        <div class="body">
          <dl id="legend-list"></dl>
        </div>
      </details>
      <div class="scroll" id="scroll" tabindex="0" role="region"
           data-i18n-aria="tableRegion">
        <table id="table">
          <caption class="sr-only" data-i18n="tableCaption"></caption>
          <thead><tr id="head"></tr></thead>
          <tbody id="rows"></tbody>
        </table>
      </div>
    </div>
  </section>

  <footer>
    <span id="footer-text"></span>
    <span data-i18n="consoleNote"></span>
    <span class="version">dotameta {{version}}</span>
  </footer>
</div>

<script>
const $ = (id) => document.getElementById(id);
const FIELDS = ["account", "bracket", "role", "position", "days", "pool", "top"];
const HAS_STRATZ = document.body.dataset.stratz === "1";

// Only text this page owns is translated. `reasons` and `warnings` arrive as
// finished sentences inside the CLI's JSON, so they stay exactly as the tool
// wrote them: translating them here would mean inventing wording the CLI never
// said, and localising the JSON itself would break its public contract.
const STRINGS = {
  en: {
    tagline: "Which heroes to spam, from your ranked All Pick record and the meta in your bracket.",
    langGroup: "Interface language",
    account: "Account id or profile URL",
    accountPlaceholder: "123456789 or opendota.com/players/123456789",
    metaBracket: "Meta bracket",
    fromRank: "From rank",
    medal1: "1 Herald",
    medal2: "2 Guardian",
    medal3: "3 Crusader",
    medal4: "4 Archon",
    medal5: "5 Legend",
    medal6: "6 Ancient",
    medal7: "7 Divine",
    medal8: "8 Immortal",
    capabilityTag: "Capability tag",
    capabilityNote: "Valve tags, not matchmaking positions",
    any: "Any",
    tagCarry: "Carry",
    tagSupport: "Support",
    tagNuker: "Nuker",
    tagDisabler: "Disabler",
    tagInitiator: "Initiator",
    tagDurable: "Durable",
    tagEscape: "Escape",
    tagPusher: "Pusher",
    metaPosition: "Meta position",
    positionNote: "Your own record is not filtered by position",
    positionUnavailable: "Needs optional Stratz setup",
    pos1: "1 Carry",
    pos2: "2 Mid",
    pos3: "3 Offlane",
    pos4: "4 Soft support",
    pos5: "5 Hard support",
    personalHistory: "Personal history",
    days30: "30 days",
    days90: "90 days",
    days365: "365 days",
    daysAll: "All history",
    maxPoolSize: "Max pool size",
    poolHint: "Projection assumes an even game split",
    tableRows: "Table rows",
    hideUnplayed: "Hide unplayed meta candidates",
    recommend: "Recommend",
    stratzSummary: "What works without a Stratz token?",
    stratzBody:
      "OpenDota needs no token and covers everything except two cases. A Stratz token is "
      + "optional, lives in STRATZ_API_TOKEN, and the tool must be restarted after setting it. "
      + "See the README for the setup.",
    stratzImmortal: "Immortal meta falls back to Divine, and the substitution is shown.",
    stratzPosition: "Positions 1-5 are unavailable: OpenDota publishes lanes, not positions.",
    emptyState: "Paste an account id or a profile URL above, then press Recommend.",
    or: "or",
    staleResults: "Results below come from the previous settings.",
    busy: "Asking OpenDota. The first run for an account takes a few seconds.",
    noPool: "No hero clears the conservative evidence threshold in this window.",
    tryYear: "Try 365 days",
    tryAll: "Try all history",
    suggestedPool: "Suggested pool",
    poolCount: "{count} of at most {max} heroes",
    statMmr100: "MMR / 100 games",
    statMmrWeek: "MMR / week",
    statMmrWeekNote: "at recent pace: {pace} games per week",
    statRank: "Rank",
    statBracket: "Meta bracket",
    statHistory: "Personal history",
    statSources: "Sources",
    statPosition: "Meta position",
    statAccount: "Account",
    openProfile: "Open profile",
    sourcePlayer: "Player",
    sourceMeta: "Meta",
    allHistory: "all history",
    days: "days",
    games: "games",
    unplayed: "unplayed",
    projected: "projected win rate",
    rankUnknown: "unknown",
    rankWindowWarning:
      "Your current rank is applied to every match in this window. A long history mixes "
      + "older patches, and possibly a different rank and role.",
    colHero: "Hero",
    colRecord: "Record",
    colWin: "Win",
    colMeta: "Meta",
    colEdge: "vs Meta",
    colMmr: "MMR / 100 low",
    colVerdict: "Verdict",
    groupYours: "Your heroes",
    groupMeta: "Meta heroes to try",
    verdictSpam: "spam",
    verdictKeep: "keep",
    verdictRisky: "risky",
    verdictLearn: "learn",
    verdictDrop: "drop",
    legendSummary: "What do the verdicts mean?",
    legendSpam: "Positive after the uncertainty discount, on enough games to trust it.",
    legendKeep: "Positive after the discount, but on a thinner record than spam.",
    legendRisky: "The blended estimate is positive, the discounted one is not yet.",
    legendLearn: "Strong in the bracket meta, with no personal record to price it.",
    legendDrop: "Even the blended estimate is below 50%.",
    whyRow: "Why {hero} is {verdict}",
    mmrRange: "MMR / 100: {low} to {high}",
    mainLane: "main lane: {lane}",
    blended: "blended {value}",
    adjusted: "adjusted {value}",
    pickShare: "pick share x{value}",
    globalTrend: "global trend {value} pp (not used in ranking)",
    metaNote: "Public per-medal aggregate from OpenDota, not documented as ranked All Pick only",
    tableRegion: "Hero recommendations, scrollable",
    tableCaption:
      "Heroes ranked by conservative MMR per 100 games, with the personal record "
      + "and the bracket meta each verdict is based on.",
    consoleNote: "Keep the dotameta console window open; closing it stops this page.",
    footer:
      "Projections assume {mmr} MMR per win and an even split across the pool. The low end "
      + "of a range is a heuristic one-standard-error haircut, not a confidence interval. "
      + "Historical win rate is not a causal estimate of future results. Open a row for the "
      + "reasons behind its verdict.",
  },
  ru: {
    tagline: "Каких героев мейнить, по твоей статистике в ранкед All Pick и мете твоего бракета.",
    langGroup: "Язык интерфейса",
    account: "ID аккаунта или ссылка на профиль",
    accountPlaceholder: "123456789 или opendota.com/players/123456789",
    metaBracket: "Бракет меты",
    fromRank: "По рангу",
    medal1: "1 Рекрут",
    medal2: "2 Страж",
    medal3: "3 Рыцарь",
    medal4: "4 Герой",
    medal5: "5 Легенда",
    medal6: "6 Властелин",
    medal7: "7 Божество",
    medal8: "8 Титан",
    capabilityTag: "Тег героя",
    capabilityNote: "Теги Valve, а не позиции в матчмейкинге",
    any: "Любой",
    tagCarry: "Керри",
    tagSupport: "Саппорт",
    tagNuker: "Нюкер",
    tagDisabler: "Дизейблер",
    tagInitiator: "Инициатор",
    tagDurable: "Танк",
    tagEscape: "Эскейп",
    tagPusher: "Пушер",
    metaPosition: "Позиция меты",
    positionNote: "Твоя личная статистика по позициям не фильтруется",
    positionUnavailable: "Нужен токен Stratz",
    pos1: "1 Керри",
    pos2: "2 Мид",
    pos3: "3 Оффлейн",
    pos4: "4 Семисапорт",
    pos5: "5 Хардсапорт",
    personalHistory: "Личная история",
    days30: "30 дней",
    days90: "90 дней",
    days365: "365 дней",
    daysAll: "Вся история",
    maxPoolSize: "Размер пула",
    poolHint: "Прогноз считает игры поровну между героями",
    tableRows: "Строк в таблице",
    hideUnplayed: "Скрыть несыгранных кандидатов",
    recommend: "Рекомендовать",
    stratzSummary: "Что работает без токена Stratz?",
    stratzBody:
      "OpenDota работает без токена и закрывает всё, кроме двух случаев. Токен Stratz "
      + "необязателен, живёт в переменной STRATZ_API_TOKEN, и после его установки программу "
      + "нужно перезапустить. Инструкция в README.",
    stratzImmortal: "Мета Титана заменяется на Божество, и подмена показывается явно.",
    stratzPosition: "Позиции 1-5 недоступны: OpenDota публикует линии, а не позиции.",
    emptyState: "Вставь ID аккаунта или ссылку на профиль и нажми «Рекомендовать».",
    or: "или",
    staleResults: "Ниже результат по прошлым настройкам.",
    busy: "Спрашиваю OpenDota. Первый запрос по аккаунту занимает несколько секунд.",
    noPool: "В этом окне ни один герой не проходит консервативный порог доказательности.",
    tryYear: "Попробовать 365 дней",
    tryAll: "Попробовать всю историю",
    suggestedPool: "Пул для спама",
    poolCount: "{count} из максимум {max} героев",
    statMmr100: "MMR / 100 игр",
    statMmrWeek: "MMR / неделю",
    statMmrWeekNote: "при недавнем темпе: {pace} игр в неделю",
    statRank: "Ранг",
    statBracket: "Бракет меты",
    statHistory: "Личная история",
    statSources: "Источники",
    statPosition: "Позиция меты",
    statAccount: "Аккаунт",
    openProfile: "Открыть профиль",
    sourcePlayer: "Игрок",
    sourceMeta: "Мета",
    allHistory: "вся история",
    days: "дней",
    games: "игр",
    unplayed: "не играл",
    projected: "прогноз винрейта",
    rankUnknown: "неизвестен",
    rankWindowWarning:
      "Текущий ранг применяется ко всем матчам окна. Длинная история смешивает старые патчи, "
      + "а возможно и другой ранг с другой ролью.",
    colHero: "Герой",
    colRecord: "Матчи",
    colWin: "Винрейт",
    colMeta: "Мета",
    colEdge: "к мете",
    colMmr: "MMR / 100 мин.",
    colVerdict: "Вердикт",
    groupYours: "Твои герои",
    groupMeta: "Мета: попробовать",
    verdictSpam: "спамить",
    verdictKeep: "оставить",
    verdictRisky: "рискованно",
    verdictLearn: "учить",
    verdictDrop: "убрать",
    legendSummary: "Что означают вердикты?",
    legendSpam: "После поправки на неуверенность плюс, и игр достаточно, чтобы этому верить.",
    legendKeep: "После поправки плюс, но статистики меньше, чем нужно для «спамить».",
    legendRisky: "Смешанная оценка плюсовая, а с поправкой ещё нет.",
    legendLearn: "Силён в мете бракета, но личной статистики нет.",
    legendDrop: "Даже смешанная оценка ниже 50%.",
    whyRow: "Почему {hero}: {verdict}",
    mmrRange: "MMR / 100: от {low} до {high}",
    mainLane: "основная линия: {lane}",
    blended: "смешанный {value}",
    adjusted: "скорректированный {value}",
    pickShare: "доля пиков x{value}",
    globalTrend: "общий тренд {value} pp (в ранжировании не участвует)",
    metaNote: "Публичная сводка по медалям из OpenDota, не только ранкед All Pick",
    tableRegion: "Рекомендации по героям, прокручиваемая таблица",
    tableCaption:
      "Герои отсортированы по консервативному MMR за 100 игр; рядом личная статистика "
      + "и мета бракета, на которых основан каждый вердикт.",
    consoleNote: "Не закрывай консольное окно dotameta: оно держит эту страницу.",
    footer:
      "Прогноз исходит из {mmr} MMR за победу и равного деления игр внутри пула. Нижняя "
      + "граница диапазона это эвристическая поправка в одну стандартную ошибку, а не "
      + "доверительный интервал. Исторический винрейт не является причинной оценкой будущего "
      + "результата. Открой строку, чтобы увидеть причины вердикта.",
  },
};

let lang = "en";
let lastData = null;

function t(key, values) {
  let text = (STRINGS[lang] && STRINGS[lang][key]) || STRINGS.en[key] || key;
  for (const [name, value] of Object.entries(values || {})) {
    text = text.split("{" + name + "}").join(value);
  }
  return text;
}

const pct = (value) => (value == null ? "-" : (value * 100).toFixed(1) + "%");
const round = (value) => (value == null ? null : Math.round(value));

function readStore(key) {
  try { return JSON.parse(localStorage.getItem(key) || "null"); } catch (error) { return null; }
}

function writeStore(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (error) { /* fine */ }
}

const VERDICT_ORDER = ["spam", "keep", "risky", "learn", "drop"];

function applyStrings() {
  document.documentElement.lang = lang;
  for (const node of document.querySelectorAll("[data-i18n]")) {
    node.textContent = t(node.dataset.i18n);
  }
  for (const node of document.querySelectorAll("[data-i18n-ph]")) {
    node.placeholder = t(node.dataset.i18nPh);
  }
  for (const node of document.querySelectorAll("[data-i18n-aria]")) {
    node.setAttribute("aria-label", t(node.dataset.i18nAria));
  }
  if (!HAS_STRATZ) $("position-note").textContent = t("positionUnavailable");
  const legend = $("legend-list");
  legend.textContent = "";
  for (const category of VERDICT_ORDER) {
    const row = document.createElement("div");
    const term = document.createElement("dt");
    term.className = "pill " + category;
    term.textContent = t("verdict" + category[0].toUpperCase() + category.slice(1));
    const description = document.createElement("dd");
    description.textContent = " " + t("legend" + category[0].toUpperCase() + category.slice(1));
    row.append(term, description);
    legend.appendChild(row);
  }
  const assumed = (lastData && lastData.plan && lastData.plan.mmr_per_win_assumed) || 25;
  $("footer-text").textContent = t("footer", { mmr: assumed }) + " ";
  for (const button of document.querySelectorAll(".langs button")) {
    button.setAttribute("aria-pressed", String(button.dataset.lang === lang));
  }
}

function setLang(next, remember) {
  lang = STRINGS[next] ? next : "en";
  if (remember) writeStore("dotameta.lang", lang);
  applyStrings();
  // A result already on screen is re-rendered rather than re-fetched: the
  // document it came from is unchanged, only its presentation is.
  if (lastData) {
    renderStats(lastData);
    renderPool(lastData);
    renderHead();
    renderRows();
  }
}

function initialLang() {
  const saved = readStore("dotameta.lang");
  if (saved && STRINGS[saved]) return saved;
  // No regular expression here on purpose: a backslash in this page is a
  // Python escape before it is ever JavaScript, so it cannot be trusted.
  const codes = (navigator.languages || [navigator.language || "en"])
    .join(",")
    .toLowerCase();
  const russian = codes.split(",").some(
    (code) => code === "ru" || code.startsWith("ru-"),
  );
  return russian ? "ru" : "en";
}

// Hero portraits are decoration. They come from Valve's CDN through the browser,
// never through the Python side, and every path below degrades to initials so
// the page stays correct offline or when the constants request fails.
const ART_HOST = "https://cdn.cloudflare.steamstatic.com";
const CONSTANTS = "https://api.opendota.com/api/constants/heroes";
const ART_KEY = "dotameta.heroart";
const ART_TTL = 7 * 24 * 3600 * 1000;
let heroArt = {};
const brokenArt = new Set();

async function loadHeroArt() {
  // Stale while revalidate: a seven day old map still names the same portraits,
  // so it is used immediately and only replaced once a fresh one actually arrives.
  const cached = readStore(ART_KEY);
  if (cached && cached.art) {
    heroArt = cached.art;
    hydrateArt();
    if (cached.saved && Date.now() - cached.saved < ART_TTL) return;
  }
  try {
    const response = await fetch(CONSTANTS, { cache: "force-cache" });
    if (!response.ok) return;
    const heroes = await response.json();
    const art = {};
    for (const hero of Object.values(heroes)) {
      if (hero && hero.id && hero.img) art[hero.id] = hero.img.split("?")[0];
    }
    if (!Object.keys(art).length) return;
    heroArt = art;
    writeStore(ART_KEY, { saved: Date.now(), art });
    hydrateArt();
  } catch (error) { /* offline: initials it is */ }
}

function hydrateArt() {
  // Portraits can arrive after a table is already on screen.
  for (const node of document.querySelectorAll("span.art[data-hero]")) {
    const id = Number(node.dataset.hero);
    if (!heroArt[id] || brokenArt.has(id)) continue;
    node.replaceWith(portrait(id, node.textContent, node.className));
  }
}

function initials(name) {
  return (name || "?")
    .split(/[ '-]+/)
    .slice(0, 2)
    .map((word) => word[0] || "")
    .join("")
    .toUpperCase();
}

function portrait(heroId, label, className) {
  const image = document.createElement("img");
  image.className = className;
  image.loading = "lazy";
  image.alt = "";
  image.src = ART_HOST + heroArt[heroId];
  image.addEventListener("error", () => {
    // One failure per hero is enough; sorting must not retry a dead CDN.
    brokenArt.add(heroId);
    image.replaceWith(letters(heroId, label, className));
  });
  return image;
}

function letters(heroId, label, className) {
  const span = document.createElement("span");
  span.className = className;
  span.dataset.hero = heroId;
  span.textContent = label;
  return span;
}

function heroArtwork(row, className) {
  const label = initials(row.name);
  if (!heroArt[row.hero_id] || brokenArt.has(row.hero_id)) {
    return letters(row.hero_id, label, className);
  }
  return portrait(row.hero_id, label, className);
}

function icon(paths, width) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", width || "2.2");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  for (const definition of paths) {
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", definition);
    svg.appendChild(path);
  }
  return svg;
}

const VERDICT_ICON = {
  spam: ["M3 17l6-6 4 4 7-7", "M14 8h6v6"],
  keep: ["M5 12l5 5 9-9"],
  risky: ["M12 10v4", "M12 17.5v.5", "M12 4L2.5 20h19z"],
  learn: ["M12 7v10", "M7 12h10"],
  drop: ["M6 6l12 12", "M18 6L6 18"],
};

const VERDICT_KEY = {
  spam: "verdictSpam",
  keep: "verdictKeep",
  risky: "verdictRisky",
  learn: "verdictLearn",
  drop: "verdictDrop",
};

// `category` stays the CLI's English value in the JSON; this is only its label.
const verdictLabel = (category) => (VERDICT_KEY[category] ? t(VERDICT_KEY[category]) : category);

function applyStratzAvailability() {
  // A control that is known to fail is worse than one that is not offered: the
  // token is a local setting, so the page can say so before a request is made.
  if (HAS_STRATZ) {
    $("stratz-help").hidden = true;
    return;
  }
  for (const option of $("position").options) {
    if (option.value) option.disabled = true;
  }
  if ($("position").value) $("position").value = "";
}

function restore() {
  const saved = readStore("dotameta.form") || {};
  for (const id of FIELDS) {
    // A configured default account wins over a stale remembered one only when
    // nothing was remembered, so the field never fights the person using it.
    if (saved[id] == null) continue;
    if (id === "account" && $(id).value) continue;
    const field = $(id);
    if (field.tagName === "SELECT") {
      const options = Array.from(field.options);
      // A remembered value from an older build may no longer be offered.
      if (!options.some((option) => option.value === saved[id])) continue;
    }
    field.value = saved[id];
  }
  $("played").checked = Boolean(saved.played);
  applyStratzAvailability();
}

function remember() {
  const saved = { played: $("played").checked };
  for (const id of FIELDS) saved[id] = $(id).value;
  writeStore("dotameta.form", saved);
}

function query() {
  const params = new URLSearchParams();
  const account = $("account").value.trim();
  if (account) params.set("account-id", account);
  for (const name of ["bracket", "role", "position", "days", "pool", "top"]) {
    const value = $(name).value.trim();
    if (value !== "") params.set(name, value);
  }
  if ($("played").checked) params.set("played-only", "1");
  return params;
}

function say(message, options) {
  const note = $("note");
  const settings = options || {};
  note.textContent = "";
  note.hidden = !message;
  note.className = "note" + (settings.error ? " error" : "");
  // An error is worth interrupting a screen reader for; progress is not.
  if (settings.error) note.setAttribute("role", "alert");
  else note.removeAttribute("role");
  if (!message) return;
  if (settings.busy) {
    const spinner = document.createElement("span");
    spinner.className = "spinner";
    note.appendChild(spinner);
  } else if (settings.error) {
    note.appendChild(icon(["M12 8v5", "M12 16.5v.5", "M12 3a9 9 0 100 18 9 9 0 000-18z"]));
  }
  const text = document.createElement("span");
  text.className = "text";
  text.textContent = message;
  note.appendChild(text);
  for (const action of settings.actions || []) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ghost";
    button.textContent = action.label;
    button.addEventListener("click", action.run);
    note.appendChild(button);
  }
}

function statCard(label, value, options) {
  const settings = options || {};
  const card = document.createElement("div");
  card.className = "stat" + (settings.headline ? " headline" : "");
  const name = document.createElement("span");
  name.textContent = label;
  card.append(name);
  if (value instanceof Node) {
    card.append(value);
  } else {
    const strong = document.createElement("strong");
    strong.textContent = value;
    card.append(strong);
  }
  if (settings.note) {
    const note = document.createElement("small");
    note.textContent = settings.note;
    card.append(note);
  }
  return card;
}

function medalOnly(code, id) {
  const label = STRINGS[code]["medal" + id] || "";
  return label.includes(" ") ? label.slice(label.indexOf(" ") + 1) : label;
}

function localizeRank(label) {
  // `rank` is the CLI's own text, such as "Legend 1". Only the medal word has a
  // known translation, so the star and anything unexpected pass through as-is.
  if (!label || lang === "en") return label;
  for (let id = 1; id <= 8; id += 1) {
    const english = medalOnly("en", id);
    if (english && label.startsWith(english)) {
      return medalOnly(lang, id) + label.slice(english.length);
    }
  }
  return label;
}

function medalName(bracketSide) {
  if (!bracketSide) return null;
  // The id is data; the name for it is chrome, so it follows the language while
  // still describing exactly the medal the CLI resolved.
  if (bracketSide.id && STRINGS[lang]["medal" + bracketSide.id]) {
    return medalOnly(lang, bracketSide.id);
  }
  return bracketSide.label;
}

function bracketCard(data) {
  const bracket = data.bracket || {};
  const resolved = medalName(bracket.resolved) || "-";
  const requested = medalName(bracket.requested);
  const strong = document.createElement("strong");
  if (bracket.fallback_applied && requested) {
    // Both stay visible: the tool answered about a bracket the user did not ask
    // for, and hiding that would hide the substitution.
    const was = document.createElement("span");
    was.className = "was";
    was.textContent = requested + " → ";
    strong.append(was, document.createTextNode(resolved));
  } else {
    strong.textContent = resolved;
  }
  return statCard(t("statBracket"), strong, { note: t("metaNote") });
}

function renderStats(data) {
  const stats = $("stats");
  stats.textContent = "";
  const plan = data.plan || {};
  const low = round(plan.mmr_per_100_conservative);
  const high = round(plan.mmr_per_100_optimistic);
  if (low != null && high != null) {
    stats.appendChild(statCard(t("statMmr100"), low + " - " + high, { headline: true }));
  }
  const weekLow = round(plan.mmr_per_week_conservative);
  const weekHigh = round(plan.mmr_per_week_optimistic);
  if (weekLow != null && weekHigh != null) {
    const pace = plan.games_per_week == null ? null : plan.games_per_week.toFixed(1);
    stats.appendChild(statCard(t("statMmrWeek"), weekLow + " - " + weekHigh, {
      note: pace == null ? null : t("statMmrWeekNote", { pace: pace }),
    }));
  }
  stats.appendChild(statCard(t("statRank"), localizeRank(data.rank) || t("rankUnknown")));
  stats.appendChild(bracketCard(data));
  const window = data.window_days ? data.window_days + " " + t("days") : t("allHistory");
  stats.appendChild(statCard(t("statHistory"), window));
  const name = (source) =>
    source === "opendota" ? "OpenDota" : source === "stratz" ? "Stratz" : source || "-";
  const sources = t("sourcePlayer") + ": " + name(data.player_source)
    + "  ·  " + t("sourceMeta") + ": " + name(data.meta_source);
  stats.appendChild(statCard(t("statSources"), sources));
  if (data.position) {
    stats.appendChild(statCard(t("statPosition"), data.position, { note: t("positionNote") }));
  }
  if (data.account_id) {
    const link = document.createElement("a");
    link.href = "https://www.opendota.com/players/" + data.account_id;
    link.target = "_blank";
    link.rel = "noreferrer noopener";
    link.textContent = t("openProfile");
    stats.appendChild(statCard(t("statAccount") + " " + data.account_id, link));
  }
}

function renderPool(data) {
  const pool = (data.plan && data.plan.pool) || [];
  const card = $("pool-card");
  card.hidden = pool.length === 0;
  if (!pool.length) return;
  const heroes = $("pool-heroes");
  heroes.textContent = "";
  for (const rec of pool) {
    const item = document.createElement("div");
    item.className = "hero-card";
    item.appendChild(heroArtwork(rec, "art"));
    const text = document.createElement("div");
    const name = document.createElement("b");
    name.textContent = rec.name;
    const detail = document.createElement("small");
    const mmr = rec.mmr_per_100_conservative;
    detail.textContent = (rec.games ? rec.games + " " + t("games") : t("unplayed"))
      + (mmr == null ? "" : "  ·  " + Math.round(mmr) + " MMR / 100");
    text.append(name, detail);
    item.appendChild(text);
    heroes.appendChild(item);
  }
  const plan = data.plan || {};
  const parts = [t("poolCount", { count: pool.length, max: $("pool").value })];
  if (plan.adjusted_winrate != null && plan.expected_winrate != null) {
    parts.push(t("projected") + " " + pct(plan.adjusted_winrate)
      + " → " + pct(plan.expected_winrate));
  }
  if (plan.pace_note) parts.push(plan.pace_note);
  $("pool-note").textContent = parts.join("  ·  ");
}

function warningItem(text) {
  const item = document.createElement("li");
  item.appendChild(icon(["M12 10v4", "M12 17.5v.5", "M12 4L2.5 20h19z"]));
  item.appendChild(document.createTextNode(text));
  return item;
}

function renderWarnings(data) {
  const list = $("warnings");
  list.textContent = "";
  const items = [];
  // Warnings are the CLI's own sentences and are shown as it wrote them.
  for (const warning of data.warnings || []) items.push(warning);
  // This one is the page's: the window is a control the page owns.
  if (data.window_days == null || data.window_days >= 365) items.push(t("rankWindowWarning"));
  list.hidden = items.length === 0;
  for (const text of items) list.appendChild(warningItem(text));
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

const COLUMNS = [
  { key: "name", label: "colHero", text: true, sort: (row) => row.name.toLowerCase() },
  { key: "games", label: "colRecord", sort: (row) => row.games || 0 },
  { key: "personal_winrate", label: "colWin", num: true, sort: (row) => row.personal_winrate },
  { key: "meta_winrate", label: "colMeta", num: true, sort: (row) => row.meta_winrate },
  { key: "edge_vs_meta", label: "colEdge", num: true, sort: (row) => edgeOf(row) },
  {
    key: "mmr",
    label: "colMmr",
    num: true,
    sort: (row) => row.mmr_per_100_conservative,
  },
  {
    key: "category",
    label: "colVerdict",
    text: true,
    // Alphabetical order would put drop above spam; the meaning has an order.
    sort: (row) => VERDICT_ORDER.indexOf(row.category),
  },
];

function edgeOf(row) {
  // A hero with no games has no personal win rate, so it has no edge over the
  // meta either. The CLI reports 0.0 there; showing that as a number, in red,
  // would be a measurement the data does not contain.
  return row.games ? row.edge_vs_meta : null;
}

let sortState = { key: null, descending: true };
let currentRows = [];
let currentPool = new Set();
let rowSerial = 0;

function cycleSort(column) {
  // Default order is the CLI ranking, so it must be reachable again: the third
  // click returns to it rather than leaving the table in an invented order.
  const first = !column.text || column.key === "category";
  if (sortState.key !== column.key) return { key: column.key, descending: first };
  if (sortState.descending === first) return { key: column.key, descending: !first };
  return { key: null, descending: true };
}

function renderHead() {
  const head = $("head");
  head.textContent = "";
  for (const column of COLUMNS) {
    const th = document.createElement("th");
    th.scope = "col";
    if (column.num) th.className = "num";
    const active = sortState.key === column.key;
    const direction = sortState.descending ? "descending" : "ascending";
    th.setAttribute("aria-sort", active ? direction : "none");
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = t(column.label);
    if (active) {
      const caret = document.createElement("span");
      caret.className = "caret";
      caret.textContent = sortState.descending ? "▾" : "▴";
      button.appendChild(caret);
    }
    button.addEventListener("click", () => {
      sortState = cycleSort(column);
      renderHead();
      renderRows();
    });
    th.appendChild(button);
    head.appendChild(th);
  }
}

function sortRows(rows) {
  if (!sortState.key) return rows;
  const column = COLUMNS.find((item) => item.key === sortState.key);
  const sorted = rows.slice();
  sorted.sort((left, right) => {
    const a = column.sort(left);
    const b = column.sort(right);
    if (a == null && b == null) return 0;
    if (a == null) return 1;
    if (b == null) return -1;
    if (a === b) return 0;
    return (a > b ? 1 : -1) * (sortState.descending ? -1 : 1);
  });
  return sorted;
}

function reasonsRow(row, id) {
  const tr = document.createElement("tr");
  tr.className = "reasons";
  tr.id = id;
  tr.hidden = true;
  const holder = document.createElement("td");
  holder.colSpan = COLUMNS.length;
  const list = document.createElement("ul");
  // Reasons are the CLI's sentences, shown verbatim in either language.
  for (const reason of row.reasons || []) {
    const item = document.createElement("li");
    item.textContent = reason;
    list.appendChild(item);
  }
  holder.appendChild(list);
  const tags = document.createElement("div");
  tags.className = "tags";
  const facts = [];
  const low = row.mmr_per_100_conservative;
  const high = row.mmr_per_100_optimistic;
  if (low != null && high != null) {
    facts.push(t("mmrRange", { low: Math.round(low), high: Math.round(high) }));
  }
  if (row.mastery) facts.push(row.mastery);
  if (row.lane) facts.push(t("mainLane", { lane: row.lane }));
  if (row.expected_winrate != null) facts.push(t("blended", { value: pct(row.expected_winrate) }));
  if (row.adjusted_winrate != null) facts.push(t("adjusted", { value: pct(row.adjusted_winrate) }));
  if (row.relative_pick_frequency != null) {
    facts.push(t("pickShare", { value: row.relative_pick_frequency.toFixed(2) }));
  }
  if (row.global_trend != null) {
    const points = row.global_trend * 100;
    // A trend of -0.04 pp printed as "-0.0" reads as a measurement of nothing.
    const trend = Math.abs(points) < 0.05 ? "0.0" : (points > 0 ? "+" : "") + points.toFixed(1);
    facts.push(t("globalTrend", { value: trend }));
  }
  for (const role of row.roles || []) facts.push(role);
  for (const fact of facts) {
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = fact;
    tags.appendChild(tag);
  }
  holder.appendChild(tags);
  tr.appendChild(holder);
  return tr;
}

function groupRow(label) {
  const tr = document.createElement("tr");
  tr.className = "group";
  const td = document.createElement("td");
  td.colSpan = COLUMNS.length;
  td.textContent = label;
  tr.appendChild(td);
  return tr;
}

function heroRow(row, best) {
  rowSerial += 1;
  const detailsId = "reasons-" + rowSerial;
  const tr = document.createElement("tr");
  tr.className = "row" + (currentPool.has(row.hero_id) ? " in-pool" : "");

  const heroCell = document.createElement("td");
  heroCell.className = "hero";
  const holder = document.createElement("div");
  holder.className = "heroname";
  const chevron = document.createElement("button");
  chevron.type = "button";
  chevron.className = "chevron";
  chevron.setAttribute("aria-expanded", "false");
  chevron.setAttribute("aria-controls", detailsId);
  chevron.setAttribute(
    "aria-label",
    t("whyRow", { hero: row.name, verdict: verdictLabel(row.category) }),
  );
  chevron.appendChild(icon(["M9 6l6 6-6 6"], "2.6"));
  holder.append(chevron, heroArtwork(row, "art"), document.createTextNode(row.name));
  heroCell.appendChild(holder);
  tr.appendChild(heroCell);

  const record = row.games ? row.wins + " / " + row.games : t("unplayed");
  tr.appendChild(cell(record, row.games ? "" : "faint"));
  const personal = row.games ? pct(row.personal_winrate) : "-";
  tr.appendChild(cell(personal, "num" + (row.games ? "" : " faint")));
  tr.appendChild(cell(pct(row.meta_winrate), "num faint"));

  const edge = edgeOf(row);
  const edgeText = edge == null ? "-" : (edge > 0 ? "+" : "") + (edge * 100).toFixed(1) + " pp";
  const edgeClass = edge == null || Math.abs(edge * 100) < 0.05
    ? "faint"
    : edge > 0 ? "up" : "down";
  tr.appendChild(cell(edgeText, "num " + edgeClass));

  const mmr = row.mmr_per_100_conservative;
  const mmrCell = cell(mmr == null ? "-" : Math.round(mmr), "num");
  if (mmr != null && mmr > 0 && best > 0) {
    const bar = document.createElement("div");
    bar.className = "bar";
    const fill = document.createElement("i");
    // Presentation only: this scales a number the CLI already computed.
    fill.style.width = Math.max(0, Math.min(100, (mmr / best) * 100)) + "%";
    bar.appendChild(fill);
    mmrCell.appendChild(bar);
  }
  tr.appendChild(mmrCell);

  const verdict = document.createElement("td");
  if (row.category) {
    const pill = document.createElement("span");
    pill.className = "pill " + row.category;
    const paths = VERDICT_ICON[row.category];
    if (paths) pill.appendChild(icon(paths));
    pill.appendChild(document.createTextNode(verdictLabel(row.category)));
    verdict.appendChild(pill);
  }
  tr.appendChild(verdict);

  const reasons = reasonsRow(row, detailsId);
  const toggle = () => {
    const open = reasons.hidden;
    reasons.hidden = !open;
    tr.classList.toggle("open", open);
    chevron.setAttribute("aria-expanded", String(open));
  };
  chevron.addEventListener("click", (event) => { event.stopPropagation(); toggle(); });
  tr.addEventListener("click", toggle);
  return [tr, reasons];
}

function renderRows() {
  const body = $("rows");
  body.textContent = "";
  const best = currentRows.reduce(
    (top, row) => Math.max(top, row.mmr_per_100_conservative || 0),
    0,
  );
  const played = currentRows.filter((row) => row.games);
  const unplayed = currentRows.filter((row) => !row.games);
  // A record you own and a hero you have never picked answer different
  // questions, so they are not interleaved unless the reader asked for an order.
  const grouped = !sortState.key && played.length && unplayed.length;
  const sections = grouped
    ? [[t("groupYours"), played], [t("groupMeta"), unplayed]]
    : [[null, sortRows(currentRows)]];
  for (const [label, rows] of sections) {
    if (label) body.appendChild(groupRow(label));
    for (const row of rows) body.append(...heroRow(row, best));
  }
}

function showSkeleton() {
  const body = $("rows");
  body.textContent = "";
  for (let index = 0; index < 6; index += 1) {
    const tr = document.createElement("tr");
    tr.className = "skeleton";
    tr.setAttribute("aria-hidden", "true");
    for (let column = 0; column < COLUMNS.length; column += 1) {
      const td = document.createElement("td");
      td.appendChild(document.createElement("i"));
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
  $("table").setAttribute("aria-busy", "true");
  $("empty").hidden = true;
  $("stale").hidden = true;
  $("result").hidden = false;
  $("stats").textContent = "";
  $("pool-card").hidden = true;
  $("warnings").hidden = true;
}

function retryWith(days) {
  return () => {
    $("days").value = String(days);
    $("form").requestSubmit();
  };
}

function noPoolActions() {
  // The next useful move is a wider window, and the page owns that control.
  const current = $("days").value;
  const actions = [];
  if (current !== "365" && current !== "0") {
    actions.push({ label: t("tryYear"), run: retryWith(365) });
  }
  if (current !== "0") actions.push({ label: t("tryAll"), run: retryWith(0) });
  return actions;
}

$("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  remember();
  $("go").disabled = true;
  showSkeleton();
  say(t("busy"), { busy: true });
  try {
    // The request carries no language: the JSON contract is the same document
    // whichever language the page happens to be showing.
    const response = await fetch("/api/recommend?" + query().toString());
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "request failed");
    lastData = data;
    currentRows = (data.recommendations || []).slice();
    const listed = new Set(currentRows.map((row) => row.hero_id));
    // A hero in the pool must be inspectable, even when `Table rows` cut the
    // list short: the pool is the answer, and the table is how it is explained.
    for (const rec of (data.plan && data.plan.pool) || []) {
      if (!listed.has(rec.hero_id)) currentRows.push(rec);
    }
    currentPool = new Set(((data.plan && data.plan.pool) || []).map((rec) => rec.hero_id));
    // A fresh answer arrives in the CLI's ranking order; keeping a stale column
    // sort would quietly re-rank it.
    sortState = { key: null, descending: true };
    const assumed = data.plan && data.plan.mmr_per_win_assumed;
    if (assumed != null) $("footer-text").textContent = t("footer", { mmr: assumed }) + " ";
    renderStats(data);
    renderPool(data);
    renderWarnings(data);
    renderHead();
    renderRows();
    $("stale").hidden = true;
    say(currentPool.size ? "" : t("noPool"), { actions: noPoolActions() });
  } catch (error) {
    // The initial hint is for someone who has not asked yet. After a submit it
    // would claim the account field is empty when it is not.
    say(String(error.message || error), { error: true });
    $("empty").hidden = true;
    $("result").hidden = lastData == null;
    $("stale").hidden = lastData == null;
    if (lastData) {
      // The skeleton replaced the table on submit. Saying "results from the
      // previous settings" while showing placeholders would be a lie.
      renderStats(lastData);
      renderPool(lastData);
      renderWarnings(lastData);
      renderHead();
      renderRows();
    }
  } finally {
    $("table").removeAttribute("aria-busy");
    $("go").disabled = false;
  }
});

for (const button of document.querySelectorAll(".langs button")) {
  button.addEventListener("click", () => setLang(button.dataset.lang, true));
}

lang = initialLang();
applyStrings();
restore();
renderHead();
loadHeroArt();
</script>
</body>
</html>
"""
