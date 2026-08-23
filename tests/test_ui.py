"""Local UI tests. The server is never bound: sockets are blocked suite-wide.

What matters here is the boundary, not the HTML: a browser must not be able to
turn a query string into an arbitrary argument, reach a command the page does
not offer, or talk to this API from another origin.
"""

from __future__ import annotations

import json
import re
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


def test_the_page_computes_nothing_itself():
    """The UI renders numbers the CLI computed; it must not compute its own."""
    for forbidden in ("MMR_PER_WIN =", "Math.sqrt", "PERSONAL_PRIOR"):
        assert forbidden not in ui.PAGE


def test_no_credential_value_ever_reaches_the_page(monkeypatch):
    """The page may name the variables in its setup help, never their contents."""
    monkeypatch.setenv("STRATZ_API_TOKEN", "stratz-token-value-0001")
    monkeypatch.setenv("OPENDOTA_API_KEY", "opendota-key-value-0002")
    page = ui.render_page()
    assert "stratz-token-value-0001" not in page
    assert "opendota-key-value-0002" not in page
    # Only the yes or no the page needs to stop offering position meta.
    assert 'data-stratz="1"' in page


def test_the_page_learns_that_stratz_is_absent():
    assert 'data-stratz=""' in ui.render_page()


# -- page delivery ---------------------------------------------------------
def test_the_footer_carries_the_running_version():
    from dotameta._version import __version__

    page = ui.render_page()
    assert "{{version}}" not in page
    assert __version__ in page


def test_the_icon_is_served_and_favicon_requests_are_not_errors():
    handler = StubHandler("/icon.svg")
    handler.do_GET()
    code, body, content_type = handler.sent[0]
    assert code == 200 and content_type == "image/svg+xml"
    assert body.startswith(b"<svg")

    handler = StubHandler("/favicon.ico")
    handler.do_GET()
    assert handler.sent[0][0] == 204
    assert handler.sent[0][1] == b""


def test_every_response_carries_the_content_security_policy():
    policy = ui.CONTENT_SECURITY_POLICY
    assert "default-src 'none'" in policy
    # Hero portraits and the id-to-portrait map are the only outside hosts.
    assert "https://cdn.cloudflare.steamstatic.com" in policy
    assert "https://api.opendota.com" in policy
    assert "script-src 'unsafe-inline'" in policy
    assert "https://" not in policy.split("script-src")[1]


def test_the_page_loads_no_third_party_code():
    """Portraits may come from a CDN; executable code may not come from anywhere."""
    assert "<script src" not in ui.PAGE
    assert '<link rel="stylesheet"' not in ui.PAGE
    # The only outside origins the page names are portraits, the id-to-portrait
    # map, and the profile link a result offers.
    origins = set(re.findall(r"https://([a-z0-9.-]+)", ui.PAGE))
    assert origins <= {
        "cdn.cloudflare.steamstatic.com",
        "api.opendota.com",
        "www.opendota.com",
        "www.w3.org",
    }


def test_a_configured_default_account_is_offered_to_the_form(monkeypatch):
    """The field can require an account only because the default is visible in it."""
    monkeypatch.setenv("DOTAMETA_ACCOUNT_ID", "123456789")
    page = ui.render_page()
    assert '<input id="account" value="123456789" required' in page
    assert "{{account}}" not in page


def test_the_account_field_is_empty_when_nothing_is_configured():
    page = ui.render_page()
    assert '<input id="account" value="" required' in page


def test_the_page_keeps_its_accessibility_affordances():
    """Each of these was a finding once; a redesign must not drop them silently."""
    page = ui.PAGE
    assert 'aria-live="polite"' in page  # loading and error announcements
    assert "aria-expanded" in page and "aria-controls" in page  # keyboard row details
    assert "aria-sort" in page  # sortable columns keep table semantics
    assert "<caption" in page and "sr-only" in page
    assert "@media (max-width: 640px)" in page  # narrow viewport, no page overflow
    assert "@media (prefers-reduced-motion: reduce)" in page
    assert "aria-busy" in page and 'aria-hidden="true"' in page


# -- two languages ---------------------------------------------------------
def language_table() -> dict[str, dict[str, str]]:
    """Pull the page's own string table out of PAGE, one dict per language."""
    block = ui.PAGE.split("const STRINGS = {", 1)[1]
    tables: dict[str, dict[str, str]] = {}
    for code in ("en", "ru"):
        body = block.split(f"\n  {code}: {{", 1)[1].split("\n  },", 1)[0]
        tables[code] = {name: "" for name in re.findall(r"^    (\w+):", body, re.MULTILINE)}
    return tables


def test_both_languages_define_the_same_strings():
    tables = language_table()
    assert tables["en"] and tables["ru"]
    missing_ru = set(tables["en"]) - set(tables["ru"])
    missing_en = set(tables["ru"]) - set(tables["en"])
    assert not missing_ru, f"no Russian for: {sorted(missing_ru)}"
    assert not missing_en, f"no English for: {sorted(missing_en)}"


def test_every_marked_element_has_a_string():
    """A `data-i18n` attribute naming a key that does not exist renders empty."""
    keys = set(language_table()["en"])
    used = set(re.findall(r'data-i18n(?:-ph|-aria)?="([^"]+)"', ui.PAGE))
    assert used, "the page marks nothing for translation"
    assert used <= keys, f"marked but never defined: {sorted(used - keys)}"


def test_every_verdict_the_cli_can_emit_has_a_label():
    keys = set(language_table()["en"])
    labels = dict(re.findall(r"^  (\w+): \"(verdict\w+)\",", ui.PAGE, re.MULTILINE))
    assert set(labels) == set(cli.CATEGORY_STYLE), "UI and CLI disagree on verdicts"
    assert set(labels.values()) <= keys


def test_the_language_never_reaches_the_command():
    """Translation is presentation: the JSON document must not depend on it."""
    with pytest.raises(ui.UiError):
        ui.build_argv("recommend", {"lang": ["ru"]})
    assert "lang" not in ui.PARAMS
    # The page fetches the same URL whichever language is showing.
    assert ui.PAGE.count('fetch("/api/recommend?" + query().toString())') == 1


def test_json_values_stay_english_in_the_page():
    """`category` is a contract value; only its label is translated."""
    assert 'pill.className = "pill " + row.category;' in ui.PAGE
    assert "verdictLabel(row.category)" in ui.PAGE


def test_the_page_contains_no_backslashes():
    """A backslash in PAGE is a Python escape long before it is JavaScript.

    A backslash-b in a regular expression arrived at the browser as a
    backspace, and backslash-question merely warned. Neither is worth
    debugging twice: the page uses string operations instead, and this keeps
    it that way.
    """
    assert chr(92) not in ui.PAGE


def test_the_history_control_offers_all_history():
    """`0 = all history` used to be a README-only secret typed into a number box."""
    page = ui.PAGE
    for value in ('value="30"', 'value="90"', 'value="365"', 'value="0"'):
        assert f"<option {value}" in page or f"<option {value} selected" in page
    assert "daysAll" in page


def test_pool_heroes_are_added_to_the_table():
    """`Table rows` must not hide a hero the plan recommends."""
    assert "if (!listed.has(rec.hero_id)) currentRows.push(rec);" in ui.PAGE
