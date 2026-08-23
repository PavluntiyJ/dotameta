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

# Serialises the redirected-stdout window below. A local UI has one user, so a
# lock is honest and cheap; without it two tabs would interleave one JSON body.
_CLI_LOCK = threading.Lock()

ROLE_PATTERN = re.compile(r"^[A-Za-z][A-Za-z ]{0,19}$")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


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
            self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
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
<style>
:root {
  color-scheme: dark;
  --bg: #12141a; --panel: #1a1d26; --line: #2b303d; --text: #e6e8ee;
  --dim: #949bad; --accent: #6ea8ff;
  --spam: #5ddc7f; --keep: #86c98f; --risky: #e8c264; --learn: #6fc9d8; --drop: #e87d7d;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.5 "Segoe UI", system-ui, sans-serif; }
header { padding: 24px 20px 8px; }
h1 { margin: 0; font-size: 20px; letter-spacing: .02em; }
header p { margin: 4px 0 0; color: var(--dim); font-size: 13px; }
main { padding: 16px 20px 48px; }
form { display: flex; flex-wrap: wrap; gap: 12px; align-items: end;
  background: var(--panel); border: 1px solid var(--line); border-radius: 10px; padding: 16px; }
label { display: flex; flex-direction: column; gap: 4px; font-size: 12px; color: var(--dim); }
input, select { background: #10121a; color: var(--text); border: 1px solid var(--line);
  border-radius: 6px; padding: 7px 9px; font: inherit; font-size: 14px; min-width: 90px; }
input:focus, select:focus { outline: 2px solid var(--accent); outline-offset: -1px; }
#account { min-width: 260px; }
button { background: var(--accent); color: #0d1017; border: 0; border-radius: 6px;
  padding: 9px 18px; font: inherit; font-weight: 600; cursor: pointer; }
button:disabled { opacity: .55; cursor: progress; }
.check { flex-direction: row; align-items: center; gap: 6px; color: var(--text); font-size: 13px; }
#status { margin: 16px 0 0; color: var(--dim); font-size: 13px; min-height: 20px; }
#status.error { color: var(--drop); }
.cards { display: flex; flex-wrap: wrap; gap: 12px; margin-top: 16px; }
.card { background: var(--panel); border: 1px solid var(--line); border-radius: 10px;
  padding: 12px 16px; min-width: 150px; }
.card span { display: block; color: var(--dim); font-size: 11px; text-transform: uppercase;
  letter-spacing: .06em; }
.card strong { font-size: 18px; font-weight: 600; }
.warnings { margin-top: 16px; padding: 0; list-style: none; }
.warnings li { color: var(--risky); font-size: 13px; padding: 3px 0; }
.scroll { overflow-x: auto; margin-top: 16px; }
table { border-collapse: collapse; width: 100%; min-width: 720px; font-size: 14px; }
th { text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: .06em;
  color: var(--dim); font-weight: 600; padding: 8px 10px; border-bottom: 1px solid var(--line); }
td { padding: 8px 10px; border-bottom: 1px solid #22262f; white-space: nowrap; }
td.num { text-align: right; font-variant-numeric: tabular-nums; }
.verdict { font-weight: 600; }
.spam { color: var(--spam); } .keep { color: var(--keep); } .risky { color: var(--risky); }
.learn { color: var(--learn); } .drop { color: var(--drop); }
footer { color: var(--dim); font-size: 12px; padding: 0 20px 32px; max-width: 760px; }
</style>
</head>
<body>
<header>
  <h1>dotameta</h1>
  <p>Which heroes to spam, from your ranked All Pick record and your bracket's meta.</p>
</header>
<main>
<form id="form">
  <label>Account id or profile URL<input id="account" placeholder="123456789"></label>
  <label>Bracket
    <select id="bracket">
      <option value="">from rank</option>
      <option value="1">1 Herald</option><option value="2">2 Guardian</option>
      <option value="3">3 Crusader</option><option value="4">4 Archon</option>
      <option value="5">5 Legend</option><option value="6">6 Ancient</option>
      <option value="7">7 Divine</option><option value="8">8 Immortal</option>
    </select>
  </label>
  <label>Role
    <select id="role">
      <option value="">any</option><option>Carry</option><option>Support</option>
      <option>Nuker</option><option>Disabler</option><option>Initiator</option>
      <option>Durable</option><option>Escape</option><option>Pusher</option>
    </select>
  </label>
  <label>Days<input id="days" type="number" min="0" max="3650" value="90"></label>
  <label>Pool<input id="pool" type="number" min="1" max="20" value="3"></label>
  <label>Show<input id="top" type="number" min="1" max="200" value="15"></label>
  <label class="check"><input id="played" type="checkbox"> played only</label>
  <button id="go" type="submit">Recommend</button>
</form>
<p id="status"></p>
<div id="cards" class="cards"></div>
<ul id="warnings" class="warnings"></ul>
<div class="scroll"><table id="table" hidden>
  <thead><tr>
    <th>Hero</th><th>Record</th><th class="num">Win</th><th class="num">Meta</th>
    <th class="num">vs Meta</th><th class="num">MMR/100</th><th>Verdict</th>
  </tr></thead>
  <tbody></tbody>
</table></div>
</main>
<footer>
  Projections assume 25 MMR per win and an even split across the pool. The low end of a
  range is a heuristic one-standard-error haircut, not a confidence interval. Historical
  win rate is not a causal estimate of future results.
</footer>
<script>
const $ = (id) => document.getElementById(id);
const pct = (value) => value == null ? "-" : (value * 100).toFixed(1) + "%";

function query() {
  const params = new URLSearchParams();
  const account = $("account").value.trim();
  if (account) params.set("account-id", account);
  for (const [name, id] of [["bracket", "bracket"], ["role", "role"], ["days", "days"],
                            ["pool", "pool"], ["top", "top"]]) {
    const value = $(id).value.trim();
    if (value !== "") params.set(name, value);
  }
  if ($("played").checked) params.set("played-only", "1");
  return params;
}

function renderCards(data) {
  const plan = data.plan || {};
  const cards = [
    ["Rank", data.rank || "unknown"],
    ["Bracket", (data.bracket && data.bracket.resolved && data.bracket.resolved.label) || "-"],
    ["Player source", data.player_source || "-"],
    ["Meta source", data.meta_source || "-"],
  ];
  const low = plan.mmr_per_100_conservative, high = plan.mmr_per_100_optimistic;
  if (low != null && high != null) {
    cards.push(["MMR / 100 games", `${Math.round(low)} to ${Math.round(high)}`]);
  }
  if (plan.mmr_per_week_conservative != null && plan.mmr_per_week_optimistic != null) {
    cards.push(["MMR / week", `${Math.round(plan.mmr_per_week_conservative)} to ` +
      `${Math.round(plan.mmr_per_week_optimistic)}`]);
  }
  $("cards").innerHTML = cards.map(([label, value]) =>
    `<div class="card"><span></span><strong></strong></div>`).join("");
  [...$("cards").children].forEach((card, index) => {
    card.querySelector("span").textContent = cards[index][0];
    card.querySelector("strong").textContent = cards[index][1];
  });
}

function renderRows(rows) {
  const body = $("table").querySelector("tbody");
  body.textContent = "";
  for (const row of rows) {
    const tr = document.createElement("tr");
    const edge = row.edge_vs_meta;
    const cells = [
      [row.name, ""],
      [row.games ? `${row.wins}/${row.games}` : "unplayed", ""],
      [row.games ? pct(row.personal_winrate) : "-", "num"],
      [pct(row.meta_winrate), "num"],
      [edge == null ? "-" : (edge > 0 ? "+" : "") + (edge * 100).toFixed(1), "num"],
      [row.mmr_per_100_conservative == null ? "-"
        : Math.round(row.mmr_per_100_conservative), "num"],
      [row.category || "", "verdict " + (row.category || "")],
    ];
    for (const [text, cls] of cells) {
      const td = document.createElement("td");
      td.textContent = text;
      if (cls) td.className = cls;
      tr.appendChild(td);
    }
    body.appendChild(tr);
  }
  $("table").hidden = rows.length === 0;
}

$("form").addEventListener("submit", async (event) => {
  event.preventDefault();
  $("go").disabled = true;
  $("status").className = "";
  $("status").textContent = "Asking OpenDota. First run for an account takes a few seconds.";
  $("warnings").textContent = "";
  try {
    const response = await fetch("/api/recommend?" + query().toString());
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "request failed");
    renderCards(data);
    renderRows(data.recommendations || []);
    for (const warning of data.warnings || []) {
      const item = document.createElement("li");
      item.textContent = warning;
      $("warnings").appendChild(item);
    }
    const pool = ((data.plan && data.plan.pool) || []).map((rec) => rec.name);
    $("status").textContent = pool.length
      ? "Suggested pool: " + pool.join(", ")
      : "No hero clears the confidence bar; no spam pool recommended.";
  } catch (error) {
    $("status").className = "error";
    $("status").textContent = String(error.message || error);
    $("table").hidden = true;
    $("cards").textContent = "";
  } finally {
    $("go").disabled = false;
  }
});
</script>
</body>
</html>
"""
