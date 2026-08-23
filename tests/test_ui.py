"""Local UI tests. The server is never bound: sockets are blocked suite-wide.

What matters here is the boundary, not the HTML: a browser must not be able to
turn a query string into an arbitrary argument, reach a command the page does
not offer, or talk to this API from another origin.
"""

from __future__ import annotations

import json
import types

import pytest

from dotameta import cli, ui
from factories import hero_row

HERO_STATS = [
    hero_row(1, "Strong Meta Hero", picks=100_000, winrate=0.55),
    hero_row(2, "Average Hero", picks=100_000, winrate=0.50),
]
HEROES = [
    {"id": 1, "localized_name": "Strong Meta Hero", "name": "npc_dota_hero_strong"},
    {"id": 2, "localized_name": "Average Hero", "name": "npc_dota_hero_average"},
]


class FakeCache:
    directory = "(fake)"

    def entries(self):
        return 0

    def clear(self):
        return 0


class FakeClient:
    calls_made = 0

    def __init__(self):
        self.cache = FakeCache()

    def hero_stats(self):
        return HERO_STATS

    def heroes(self):
        return HEROES

    def player(self, account_id):
        return {"rank_tier": 51, "profile": {"personaname": "Tester"}}

    def player_win_loss(self, account_id, **filters):
        return {"win": 60, "lose": 40}

    def player_heroes(self, account_id, **filters):
        return [
            {"hero_id": 1, "games": 120, "win": 70},
            {"hero_id": 2, "games": 40, "win": 18},
        ]

    def player_matches(self, account_id, limit=100, **filters):
        return []


# -- argv translation ------------------------------------------------------
def test_query_becomes_the_argv_a_terminal_user_would_type():
    argv = ui.build_argv(
        "recommend",
        {"account-id": ["123456789"], "bracket": ["7"], "role": ["Support"], "played-only": ["1"]},
    )
    assert argv[0] == "recommend"
    assert argv[-1] == "--json"
    assert "--account-id" in argv and "123456789" in argv
    assert argv[argv.index("--bracket") + 1] == "7"
    assert argv[argv.index("--role") + 1] == "Support"
    assert "--played-only" in argv


def test_an_unchecked_box_adds_no_flag():
    assert "--played-only" not in ui.build_argv("recommend", {"played-only": ["0"]})


def test_blank_values_are_dropped_rather_than_passed_as_empty_flags():
    assert ui.build_argv("recommend", {"bracket": [""], "role": ["  "]}) == [
        "recommend",
        "--json",
    ]


@pytest.mark.parametrize(
    "query",
    [
        {"cache-dir": ["/etc"]},  # not on the allowlist at all
        {"heroes-file": ["secrets.txt"]},  # a real flag, still not offered by the page
        {"bracket": ["7", "8"]},  # repeated parameter
        {"bracket": ["9"]},  # out of range
        {"bracket": ["--no-cache"]},  # a flag smuggled in as a value
        {"top": ["1e3"]},
        {"role": ["--json"]},
        {"role": ["Support; rm -rf /"]},
        {"source": ["dotabuff"]},
        {"account-id": ["https://evil.example.com/players/123456789"]},
    ],
)
def test_a_browser_cannot_invent_arguments(query):
    with pytest.raises(ui.UiError):
        ui.build_argv("recommend", query)


def test_commands_only_accept_their_own_parameters():
    with pytest.raises(ui.UiError):
        ui.build_argv("meta", {"account-id": ["123456789"]})
    with pytest.raises(ui.UiError):
        ui.build_argv("cache", {})  # not exposed to the page
    assert ui.build_argv("meta", {"bracket": ["7"]}) == ["meta", "--bracket", "7", "--json"]


# -- running the real command ----------------------------------------------
def test_the_page_is_served_the_cli_json_document(monkeypatch):
    monkeypatch.setattr(cli, "build_client", lambda args, settings: FakeClient())
    code, payload = ui.run_json(ui.build_argv("recommend", {"account-id": ["123456789"]}))
    assert code == 0
    assert payload["schema_version"] == cli.SCHEMA_VERSION
    assert payload["player_source"] == "opendota"
    assert [rec["name"] for rec in payload["recommendations"]]


def test_a_failed_command_reports_its_message_and_no_document(monkeypatch):
    monkeypatch.setattr(cli, "build_client", lambda args, settings: FakeClient())
    # Explicit Stratz without --bracket is a CliError, so stdout must stay empty.
    code, payload = ui.run_json(
        ui.build_argv("recommend", {"account-id": ["123456789"], "source": ["stratz"]})
    )
    assert code != 0
    assert "error" in payload and "schema_version" not in payload


def test_a_crashing_command_does_not_escape_into_the_server(monkeypatch):
    def explode(argv):
        raise RuntimeError("boom")

    monkeypatch.setattr(cli, "main", explode)
    code, payload = ui.run_json(["recommend", "--json"])
    assert code == 2
    assert "boom" in payload["error"]


# -- origin and routing ----------------------------------------------------
@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1:8765", True),
        ("localhost:8765", True),
        ("[::1]:8765", True),
        ("127.0.0.1", True),
        ("dotameta.example.com:8765", False),
        ("192.168.1.14:8765", False),
        ("", False),
        (None, False),
    ],
)
def test_only_loopback_hosts_are_answered(host, expected):
    assert ui._host_is_local(host, 8765) is expected


class StubHandler(ui.Handler):
    """A handler with the socket machinery replaced, so routing can be tested."""

    def __init__(self, path: str, host: str = "127.0.0.1:8765"):
        self.path = path
        self.headers = {"Host": host}
        self.server = types.SimpleNamespace(server_address=("127.0.0.1", 8765))
        self.sent: list[tuple[int, bytes, str]] = []

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.sent.append((code, body, content_type))


def test_the_root_path_serves_the_page():
    handler = StubHandler("/")
    handler.do_GET()
    code, body, content_type = handler.sent[0]
    assert code == 200
    assert content_type.startswith("text/html")
    assert b"<title>dotameta</title>" in body


def test_a_rebinding_host_is_refused_before_anything_runs():
    handler = StubHandler("/api/recommend?account-id=123456789", host="evil.example.com")
    handler.do_GET()
    code, body, _ = handler.sent[0]
    assert code == 403
    assert "loopback" in json.loads(body)["error"]


def test_a_bad_parameter_is_a_request_error_not_a_command_run():
    handler = StubHandler("/api/recommend?cache-dir=/etc")
    handler.do_GET()
    code, body, _ = handler.sent[0]
    assert code == 400
    assert "cache-dir" in json.loads(body)["error"]


def test_unknown_paths_are_not_routed_to_a_command():
    handler = StubHandler("/api/cache")
    handler.do_GET()
    assert handler.sent[0][0] == 400
    handler = StubHandler("/wp-admin")
    handler.do_GET()
    assert handler.sent[0][0] == 404


def test_the_page_carries_no_credentials_and_no_scoring():
    """The UI renders numbers the CLI computed; it must not compute its own."""
    for forbidden in ("STRATZ_API_TOKEN", "OPENDOTA_API_KEY", "MMR_PER_WIN =", "Math.sqrt"):
        assert forbidden not in ui.PAGE
