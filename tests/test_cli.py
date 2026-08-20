"""Command-level tests driven through a fake client. Never touches the network."""

from __future__ import annotations

import argparse
import json

import pytest

from dotameta import cli
from dotameta.cli import build_parser, main, parse_account_id
from factories import hero_row

HERO_STATS = [
    hero_row(1, "Strong Meta Hero", picks=100_000, winrate=0.55),
    hero_row(2, "Average Hero", picks=100_000, winrate=0.50),
    hero_row(3, "Weak Hero", picks=100_000, winrate=0.45),
]
HEROES = [
    {"id": 1, "localized_name": "Strong Meta Hero", "name": "npc_dota_hero_strong"},
    {"id": 2, "localized_name": "Average Hero", "name": "npc_dota_hero_average"},
    {"id": 3, "localized_name": "Weak Hero", "name": "npc_dota_hero_weak"},
]


class FakeCache:
    directory = "(fake)"

    def entries(self):
        return 0

    def clear(self):
        return 0


class FakeClient:
    calls_made = 0

    def __init__(self, hero_rows=None, matches=None):
        self.hero_rows = hero_rows or []
        self.matches = matches or []
        self.cache = FakeCache()

    def hero_stats(self):
        return HERO_STATS

    def heroes(self):
        return HEROES

    def player(self, account_id):
        return {"rank_tier": 51, "profile": {"personaname": "Tester"}}

    def player_win_loss(self, account_id, **filters):
        return {"win": 10, "lose": 10}

    def player_heroes(self, account_id, **filters):
        return self.hero_rows

    def player_matches(self, account_id, limit=100, **filters):
        return self.matches


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeClient()
    monkeypatch.setattr(cli, "build_client", lambda args, settings: client)
    return client


def run(argv, capsys):
    code = main(argv)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# -- argument validation (before any request) ------------------------------
@pytest.mark.parametrize(
    "argv",
    [
        ["recommend", "--top", "-1"],
        ["recommend", "--pool", "0"],
        ["recommend", "--days", "-5"],
        ["recommend", "--min-picks", "-1"],
        ["recommend", "--min-games", "-3"],
        ["--cache-ttl", "-1", "recommend"],
    ],
)
def test_negative_numeric_arguments_are_rejected(argv):
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


def test_a_long_match_id_in_a_url_does_not_become_the_account_id():
    """Regression: the parser took the longest digit run anywhere in the string."""
    url = "https://www.opendota.com/players/123456789?matchId=7891234567890123"
    assert parse_account_id(url) == 123456789


@pytest.mark.parametrize(
    "value",
    [
        "123456789",
        "https://www.opendota.com/players/123456789",
        "https://www.dotabuff.com/players/123456789/matches",
        "https://stratz.com/players/123456789",
        "opendota.com/players/123456789/heroes",
    ],
)
def test_known_profile_urls_resolve(value):
    assert parse_account_id(value) == 123456789


def test_steam64_is_converted():
    assert parse_account_id("https://steamcommunity.com/profiles/76561198083722517/") == 123456789


@pytest.mark.parametrize(
    "value",
    [
        "https://evil.example.com/players/123456789",
        "https://www.dotabuff.com/players/mrbeast",
        "https://www.dotabuff.com/esports/leagues/12345",
        "nonsense",
        "0",
    ],
)
def test_unusable_account_inputs_are_rejected(value):
    with pytest.raises(argparse.ArgumentTypeError):
        parse_account_id(value)


# -- paste path ------------------------------------------------------------
def test_paste_and_heroes_file_are_mutually_exclusive(tmp_path, capsys, fake_client):
    path = tmp_path / "heroes.txt"
    path.write_text("Average Hero 100 55%", encoding="utf-8")
    code, out, err = run(
        ["recommend", "--paste", "--heroes-file", str(path), "--bracket", "5"], capsys
    )
    assert code == 2
    assert "not both" in err
    assert out == ""


def test_paste_requires_a_bracket(tmp_path, capsys, fake_client):
    path = tmp_path / "heroes.txt"
    path.write_text("Average Hero 100 55%", encoding="utf-8")
    code, out, err = run(["recommend", "--heroes-file", str(path)], capsys)
    assert code == 2
    assert "--bracket is required" in err


def test_a_missing_file_is_one_line_not_a_traceback(capsys, fake_client):
    code, out, err = run(
        ["recommend", "--heroes-file", "no-such-file.txt", "--bracket", "5"], capsys
    )
    assert code == 2
    assert "no such file" in err
    assert "Traceback" not in err


def test_a_paste_with_no_recognisable_heroes_fails(tmp_path, capsys, fake_client):
    """Regression: it used to succeed as a meta-only recommendation."""
    path = tmp_path / "heroes.txt"
    path.write_text("total games 500\nwinrate 51%\n", encoding="utf-8")
    code, out, err = run(["recommend", "--heroes-file", str(path), "--bracket", "5"], capsys)
    assert code == 2
    assert "no heroes recognised" in err


def test_paste_json_reports_unmatched_lines_and_a_null_account(tmp_path, capsys, fake_client):
    path = tmp_path / "heroes.txt"
    path.write_text("Average Hero 200 60%\nsome junk line\n", encoding="utf-8")
    code, out, err = run(
        ["recommend", "--heroes-file", str(path), "--bracket", "5", "--json"], capsys
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["source"] == "paste"
    assert payload["account_id"] is None
    assert payload["window_days"] is None
    assert payload["paste"]["unmatched_lines"] == ["some junk line"]


# -- JSON contract ---------------------------------------------------------
def test_json_stdout_is_pure_json(capsys, fake_client):
    fake_client.hero_rows = [{"hero_id": 2, "games": 200, "win": 110}]
    code, out, err = run(["recommend", "--account-id", "123456789", "--json"], capsys)
    assert code == 0
    payload = json.loads(out)  # would raise if anything else were printed
    assert payload["schema_version"] == 1
    assert payload["source"] == "account"
    assert payload["mode"] == "personal"
    assert payload["data_status"] == "available"


def test_json_exposes_bracket_fallback(capsys, fake_client):
    # HERO_STATS only populates medal 5, so asking for 8 must fall back and say so.
    code, out, err = run(
        ["recommend", "--account-id", "123456789", "--bracket", "8", "--json"], capsys
    )
    payload = json.loads(out)
    assert payload["bracket"]["requested"]["id"] == 8
    assert payload["bracket"]["resolved"]["id"] == 5
    assert payload["bracket"]["fallback_applied"] is True
    assert any("using" in w for w in payload["warnings"])


def test_json_marks_meta_only_mode_for_a_private_profile(capsys, fake_client):
    fake_client.hero_rows = []
    code, out, err = run(["recommend", "--account-id", "123456789", "--json"], capsys)
    payload = json.loads(out)
    assert payload["mode"] == "meta_only"
    assert payload["data_status"] == "private_or_unavailable"


def test_json_pool_entries_are_full_records_even_when_top_is_small(capsys, fake_client):
    fake_client.hero_rows = [{"hero_id": 2, "games": 300, "win": 170}]
    code, out, err = run(["recommend", "--account-id", "123456789", "--top", "1", "--json"], capsys)
    payload = json.loads(out)
    for entry in payload["plan"]["pool"]:
        assert {"hero_id", "adjusted_winrate", "category"} <= set(entry)


def test_every_command_supports_json(capsys, fake_client):
    for argv in (
        ["meta", "--json"],
        ["player", "--account-id", "123456789", "--json"],
        ["cache", "--json"],
    ):
        code, out, err = run(argv, capsys)
        assert code == 0, argv
        assert json.loads(out)["schema_version"] == 1


def test_errors_go_to_stderr_and_leave_stdout_empty(capsys, fake_client):
    code, out, err = run(["recommend"], capsys)
    assert code == 2
    assert out == ""
    assert "No account id" in err


# -- meta source selection -------------------------------------------------
def test_auto_stays_on_opendota_without_a_stratz_token(capsys, fake_client):
    """A token nobody has must never be required for the default path."""
    code, out, err = run(["recommend", "--account-id", "123456789", "--json"], capsys)
    assert json.loads(out)["meta_source"] == "opendota"


def test_asking_for_immortal_without_a_token_falls_back_and_says_so(capsys, fake_client):
    code, out, err = run(
        ["recommend", "--account-id", "123456789", "--bracket", "8", "--json"], capsys
    )
    payload = json.loads(out)
    assert payload["meta_source"] == "opendota"
    assert payload["bracket"]["fallback_applied"] is True
    assert any("STRATZ_API_TOKEN" in w for w in payload["warnings"])


def test_position_without_a_token_warns_rather_than_pretending(capsys, fake_client):
    code, out, err = run(
        ["recommend", "--account-id", "123456789", "--position", "4", "--json"], capsys
    )
    payload = json.loads(out)
    assert payload["meta_source"] == "opendota"
    assert payload["position"] is None
    assert any("positions 1-5" in w for w in payload["warnings"])


def test_forcing_stratz_without_a_token_is_a_clear_error(capsys, fake_client):
    code, out, err = run(["recommend", "--account-id", "123456789", "--source", "stratz"], capsys)
    assert code == 2
    assert "STRATZ_API_TOKEN" in err
    assert out == ""


def test_auto_uses_stratz_for_immortal_when_a_token_exists(capsys, fake_client, monkeypatch):
    from dotameta.config import Settings

    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(stratz_token="tok")))

    class FakeStratz:
        def __init__(self, *args, **kwargs):
            pass

        def hero_win_rates(self, medal, position=None):
            assert medal == 8  # the bracket OpenDota cannot answer
            return [{"heroId": 1, "matchCount": 50_000, "winCount": 28_000}]

    monkeypatch.setattr(cli, "StratzClient", FakeStratz)
    code, out, err = run(
        ["recommend", "--account-id", "123456789", "--bracket", "8", "--json"], capsys
    )
    payload = json.loads(out)
    assert payload["meta_source"] == "stratz"
    assert payload["bracket"]["resolved"]["id"] == 8
