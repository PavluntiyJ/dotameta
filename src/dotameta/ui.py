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
    """The page, with the running version and any configured account filled in.

    `DOTAMETA_ACCOUNT_ID` is documented as a convenience default rather than a
    credential, and the CLI already applies it. Showing it means an empty field
    really does mean "no account", which is what lets the form require one.
    """
    account = Settings.from_env().account_id
    return PAGE.replace("{{version}}", __version__).replace(
        "{{account}}", str(account) if account else ""
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
  grid-template-columns: repeat(auto-fit, minmax(118px, 1fr));
}
.field { display: flex; flex-direction: column; gap: 6px; min-width: 0; }
.field--wide { grid-column: span 3; min-width: 200px; }
.field > span {
  font-size: 11px;
  letter-spacing: .07em;
  text-transform: uppercase;
  color: var(--dim);
}
.field > span em {
  font-style: normal;
  text-transform: none;
  letter-spacing: 0;
  color: var(--faint);
}
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

/* feedback */
.note {
  display: flex;
  align-items: flex-start;
  gap: 10px;
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
<body>
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
      <p>Which heroes to spam, from your ranked All Pick record and the meta in your bracket.</p>
    </div>
  </div>

  <form class="panel" id="form">
    <div class="fields">
      <label class="field field--wide">
        <span>Account id or profile URL</span>
        <input id="account" value="{{account}}" required
               placeholder="123456789 or opendota.com/players/123456789"
               autocomplete="off" spellcheck="false">
      </label>
      <label class="field">
        <span>Bracket</span>
        <select id="bracket">
          <option value="">From rank</option>
          <option value="1">1 Herald</option><option value="2">2 Guardian</option>
          <option value="3">3 Crusader</option><option value="4">4 Archon</option>
          <option value="5">5 Legend</option><option value="6">6 Ancient</option>
          <option value="7">7 Divine</option><option value="8">8 Immortal</option>
        </select>
      </label>
      <label class="field">
        <span>Hero tag</span>
        <select id="role">
          <option value="">Any</option><option>Carry</option><option>Support</option>
          <option>Nuker</option><option>Disabler</option><option>Initiator</option>
          <option>Durable</option><option>Escape</option><option>Pusher</option>
        </select>
      </label>
      <label class="field">
        <span>Position <em>Stratz token</em></span>
        <select id="position">
          <option value="">Any</option>
          <option value="1">1 Carry</option><option value="2">2 Mid</option>
          <option value="3">3 Offlane</option><option value="4">4 Soft support</option>
          <option value="5">5 Hard support</option>
        </select>
      </label>
      <label class="field">
        <span>History <em>days</em></span>
        <input id="days" type="number" min="0" max="3650" value="90">
      </label>
      <label class="field">
        <span>Pool size</span>
        <input id="pool" type="number" min="1" max="20" value="3">
      </label>
      <label class="field">
        <span>Table rows</span>
        <input id="top" type="number" min="1" max="200" value="15">
      </label>
    </div>
    <div class="actions">
      <label class="toggle">
        <input id="played" type="checkbox"><span>Played heroes only</span>
      </label>
      <button id="go" type="submit">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4"
             stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M3 17l6-6 4 4 7-7"/><path d="M14 8h6v6"/>
        </svg>
        Recommend
      </button>
    </div>
  </form>

  <p class="note" id="note" aria-live="polite" aria-atomic="true" hidden></p>

  <div class="empty" id="empty">
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6"
         stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <circle cx="11" cy="11" r="7"/><path d="M20 20l-4.2-4.2"/>
    </svg>
    Paste an account id or a profile URL above, then press Recommend.<br>
    <code>123456789</code> or <code>opendota.com/players/123456789</code>
  </div>

  <section id="result" hidden>
    <div class="stats" id="stats"></div>
    <div class="pool" id="pool-card" hidden>
      <h2>Suggested pool</h2>
      <div class="pool-heroes" id="pool-heroes"></div>
      <p class="pool-note" id="pool-note"></p>
    </div>
    <ul class="warnings" id="warnings" hidden></ul>
    <div class="tablecard">
      <div class="scroll" id="scroll" tabindex="0" role="region"
           aria-label="Hero recommendations, scrollable">
        <table id="table">
          <caption class="sr-only">
            Heroes ranked by conservative MMR per 100 games, with the personal
            record and the bracket meta each verdict is based on.
          </caption>
          <thead><tr id="head"></tr></thead>
          <tbody id="rows"></tbody>
        </table>
      </div>
    </div>
  </section>

  <footer>
    Projections assume <span id="assumed">25</span> MMR per win and an even split across the
    pool. The low end of a range is a heuristic one-standard-error haircut, not a confidence
    interval. Historical win rate is not a causal estimate of future results. Open a row for the
    reasons behind its verdict. <span class="version">dotameta {{version}}</span>
  </footer>
</div>

<script>
const $ = (id) => document.getElementById(id);
const FIELDS = ["account", "bracket", "role", "position", "days", "pool", "top"];
const pct = (value) => (value == null ? "-" : (value * 100).toFixed(1) + "%");
const round = (value) => (value == null ? null : Math.round(value));

// Hero portraits are decoration. They come from Valve's CDN through the browser,
// never through the Python side, and every path below degrades to initials so
// the page stays correct offline or when the constants request fails.
const ART_HOST = "https://cdn.cloudflare.steamstatic.com";
const CONSTANTS = "https://api.opendota.com/api/constants/heroes";
const ART_KEY = "dotameta.heroart";
const ART_TTL = 7 * 24 * 3600 * 1000;
let heroArt = {};
const brokenArt = new Set();

function readStore(key) {
  try { return JSON.parse(localStorage.getItem(key) || "null"); } catch (error) { return null; }
}

function writeStore(key, value) {
  try { localStorage.setItem(key, JSON.stringify(value)); } catch (error) { /* fine */ }
}

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

function restore() {
  const saved = readStore("dotameta.form") || {};
  for (const id of FIELDS) {
    // A configured default account wins over a stale remembered one only when
    // nothing was remembered, so the field never fights the person using it.
    if (saved[id] != null && (id !== "account" || !$(id).value)) $(id).value = saved[id];
  }
  $("played").checked = Boolean(saved.played);
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
  note.appendChild(document.createTextNode(message));
}

function statCard(label, value, headline) {
  const card = document.createElement("div");
  card.className = "stat" + (headline ? " headline" : "");
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
  return card;
}

function bracketCard(data) {
  const bracket = data.bracket || {};
  const resolved = (bracket.resolved && bracket.resolved.label) || "-";
  const requested = bracket.requested && bracket.requested.label;
  const strong = document.createElement("strong");
  if (bracket.fallback_applied && requested) {
    // Both numbers stay visible: the tool answered about a bracket the user
    // did not ask for, and hiding that would hide the substitution.
    const was = document.createElement("span");
    was.className = "was";
    was.textContent = requested + " → ";
    strong.append(was, document.createTextNode(resolved));
  } else {
    strong.textContent = resolved;
  }
  return statCard("Bracket", strong);
}

function renderStats(data) {
  const stats = $("stats");
  stats.textContent = "";
  const plan = data.plan || {};
  const low = round(plan.mmr_per_100_conservative);
  const high = round(plan.mmr_per_100_optimistic);
  if (low != null && high != null) {
    stats.appendChild(statCard("MMR / 100 games", low + " to " + high, true));
  }
  const weekLow = round(plan.mmr_per_week_conservative);
  const weekHigh = round(plan.mmr_per_week_optimistic);
  if (weekLow != null && weekHigh != null) {
    stats.appendChild(statCard("MMR / week", weekLow + " to " + weekHigh));
  }
  stats.appendChild(statCard("Rank", data.rank || "unknown"));
  stats.appendChild(bracketCard(data));
  const window = data.window_days ? data.window_days + " days" : "all history";
  stats.appendChild(statCard("History", window));
  const label = (name) => (name === "opendota" ? "OpenDota" : name === "stratz" ? "Stratz" : name);
  const sources = "Player: " + label(data.player_source || "-")
    + "  ·  Meta: " + label(data.meta_source || "-");
  stats.appendChild(statCard("Sources", sources));
  if (data.position) stats.appendChild(statCard("Position", data.position));
  if (data.account_id) {
    const link = document.createElement("a");
    link.href = "https://www.opendota.com/players/" + data.account_id;
    link.target = "_blank";
    link.rel = "noreferrer noopener";
    link.textContent = "Open profile";
    stats.appendChild(statCard("Account " + data.account_id, link));
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
    detail.textContent = (rec.games ? rec.games + " games" : "unplayed")
      + (mmr == null ? "" : "  ·  " + Math.round(mmr) + " MMR / 100");
    text.append(name, detail);
    item.appendChild(text);
    heroes.appendChild(item);
  }
  const plan = data.plan || {};
  const parts = [];
  if (plan.adjusted_winrate != null && plan.expected_winrate != null) {
    parts.push("projected win rate " + pct(plan.adjusted_winrate)
      + " → " + pct(plan.expected_winrate));
  }
  if (plan.games_per_week != null) parts.push(plan.games_per_week.toFixed(1) + " games per week");
  if (plan.pace_note) parts.push(plan.pace_note);
  $("pool-note").textContent = parts.join("  ·  ");
}

function renderWarnings(data) {
  const list = $("warnings");
  list.textContent = "";
  const warnings = data.warnings || [];
  list.hidden = warnings.length === 0;
  for (const warning of warnings) {
    const item = document.createElement("li");
    item.appendChild(icon(["M12 10v4", "M12 17.5v.5", "M12 4L2.5 20h19z"]));
    item.appendChild(document.createTextNode(warning));
    list.appendChild(item);
  }
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

const COLUMNS = [
  { key: "name", label: "Hero", text: true, sort: (row) => row.name.toLowerCase() },
  { key: "games", label: "Record", sort: (row) => row.games || 0 },
  { key: "personal_winrate", label: "Win", num: true, sort: (row) => row.personal_winrate },
  { key: "meta_winrate", label: "Meta", num: true, sort: (row) => row.meta_winrate },
  { key: "edge_vs_meta", label: "vs Meta", num: true, sort: (row) => row.edge_vs_meta },
  {
    key: "mmr",
    label: "MMR / 100 low",
    num: true,
    sort: (row) => row.mmr_per_100_conservative,
  },
  { key: "category", label: "Verdict", text: true, sort: (row) => row.category || "" },
];

let sortState = { key: null, descending: true };
let currentRows = [];
let currentPool = new Set();
let rowSerial = 0;

function cycleSort(column) {
  // Default order is the CLI ranking, so it must be reachable again: the third
  // click returns to it rather than leaving the table in an invented order.
  const first = column.text ? false : true;
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
    button.textContent = column.label;
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

function sortedRows() {
  if (!sortState.key) return currentRows;
  const column = COLUMNS.find((item) => item.key === sortState.key);
  const rows = currentRows.slice();
  rows.sort((left, right) => {
    const a = column.sort(left);
    const b = column.sort(right);
    if (a == null && b == null) return 0;
    if (a == null) return 1;
    if (b == null) return -1;
    if (a === b) return 0;
    return (a > b ? 1 : -1) * (sortState.descending ? -1 : 1);
  });
  return rows;
}

function reasonsRow(row, id) {
  const tr = document.createElement("tr");
  tr.className = "reasons";
  tr.id = id;
  tr.hidden = true;
  const holder = document.createElement("td");
  holder.colSpan = COLUMNS.length;
  const list = document.createElement("ul");
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
    facts.push("MMR / 100: " + Math.round(low) + " to " + Math.round(high));
  }
  if (row.mastery) facts.push(row.mastery);
  if (row.lane) facts.push("main lane: " + row.lane);
  if (row.expected_winrate != null) facts.push("blended " + pct(row.expected_winrate));
  if (row.adjusted_winrate != null) facts.push("adjusted " + pct(row.adjusted_winrate));
  if (row.relative_pick_frequency != null) {
    facts.push("pick share x" + row.relative_pick_frequency.toFixed(2));
  }
  if (row.global_trend != null) {
    facts.push("global trend " + (row.global_trend > 0 ? "+" : "")
      + (row.global_trend * 100).toFixed(1) + " pp (not used in ranking)");
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

function renderRows() {
  const body = $("rows");
  body.textContent = "";
  const rows = sortedRows();
  const best = rows.reduce((top, row) => Math.max(top, row.mmr_per_100_conservative || 0), 0);
  for (const row of rows) {
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
    chevron.setAttribute("aria-label", "Why " + row.name + " is " + (row.category || "listed"));
    chevron.appendChild(icon(["M9 6l6 6-6 6"], "2.6"));
    holder.append(chevron, heroArtwork(row, "art"), document.createTextNode(row.name));
    heroCell.appendChild(holder);
    tr.appendChild(heroCell);

    const record = row.games ? row.wins + " / " + row.games : "unplayed";
    tr.appendChild(cell(record, row.games ? "" : "faint"));
    const personal = row.games ? pct(row.personal_winrate) : "-";
    tr.appendChild(cell(personal, "num" + (row.games ? "" : " faint")));
    tr.appendChild(cell(pct(row.meta_winrate), "num faint"));

    const edge = row.edge_vs_meta;
    const edgeText = edge == null
      ? "-"
      : (edge > 0 ? "+" : "") + (edge * 100).toFixed(1) + " pp";
    tr.appendChild(cell(edgeText, "num " + (edge == null ? "faint" : edge > 0 ? "up" : "down")));

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
      pill.appendChild(document.createTextNode(row.category));
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
    body.append(tr, reasons);
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
  $("result").hidden = false;
  $("stats").textContent = "";
  $("pool-card").hidden = true;
  $("warnings").hidden = true;
}

$("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  remember();
  $("go").disabled = true;
  showSkeleton();
  say("Asking OpenDota. The first run for an account takes a few seconds.", { busy: true });
  try {
    const response = await fetch("/api/recommend?" + query().toString());
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "request failed");
    currentRows = data.recommendations || [];
    currentPool = new Set(((data.plan && data.plan.pool) || []).map((rec) => rec.hero_id));
    // A fresh answer arrives in the CLI's ranking order; keeping a stale column
    // sort would quietly re-rank it.
    sortState = { key: null, descending: true };
    const assumed = data.plan && data.plan.mmr_per_win_assumed;
    if (assumed != null) $("assumed").textContent = assumed;
    renderStats(data);
    renderPool(data);
    renderWarnings(data);
    renderHead();
    renderRows();
    say(currentPool.size ? "" : "No hero clears the confidence bar, so no spam pool is suggested.");
  } catch (error) {
    say(String(error.message || error), { error: true });
    $("result").hidden = true;
    $("empty").hidden = false;
  } finally {
    $("table").removeAttribute("aria-busy");
    $("go").disabled = false;
  }
});

restore();
renderHead();
loadHeroArt();
</script>
</body>
</html>
"""
