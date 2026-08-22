"""Command tests using the synthetic account 123456789. Never touches the network."""

from __future__ import annotations

import argparse
import json
import time

import pytest

from dotameta import cli
from dotameta.cache import Cache
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
        self.calls = []
        self.cache = FakeCache()

    def hero_stats(self):
        self.calls.append("hero_stats")
        return HERO_STATS

    def heroes(self):
        self.calls.append("heroes")
        return HEROES

    def player(self, account_id):
        self.calls.append("player")
        return {"rank_tier": 51, "profile": {"personaname": "Tester"}}

    def player_win_loss(self, account_id, **filters):
        self.calls.append("player_win_loss")
        return {"win": 10, "lose": 10}

    def player_heroes(self, account_id, **filters):
        self.calls.append("player_heroes")
        return self.hero_rows

    def player_matches(self, account_id, limit=100, **filters):
        self.calls.append("player_matches")
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
        "https://www.dotabuff.com/players/not-a-number",
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


def test_paste_with_explicit_stratz_requires_a_token_before_hero_requests(
    tmp_path, capsys, fake_client
):
    path = tmp_path / "heroes.txt"
    path.write_text("Average Hero 100 55%", encoding="utf-8")

    code, out, err = run(
        [
            "recommend",
            "--heroes-file",
            str(path),
            "--bracket",
            "5",
            "--source",
            "stratz",
            "--json",
        ],
        capsys,
    )

    assert code == 2
    assert out == ""
    assert "STRATZ_API_TOKEN" in err
    assert fake_client.calls == []


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
    assert any(
        "assume" in warning and "ranked All Pick" in warning for warning in payload["warnings"]
    )


def test_human_paste_warns_about_mode_and_does_not_claim_ranked_totals(
    tmp_path, capsys, fake_client
):
    path = tmp_path / "heroes.txt"
    path.write_text("Average Hero 200 60%\n", encoding="utf-8")

    code, out, err = run(["recommend", "--heroes-file", str(path), "--bracket", "5"], capsys)

    assert code == 0
    assert "200 games in pasted table" in out
    assert "200 ranked games" not in out
    assert "assume the table was" in err
    assert "filtered to ranked All Pick" in err


# -- JSON contract ---------------------------------------------------------
def test_json_stdout_is_pure_json(capsys, fake_client):
    fake_client.hero_rows = [{"hero_id": 2, "games": 200, "win": 110}]
    code, out, err = run(["recommend", "--account-id", "123456789", "--json"], capsys)
    assert code == 0
    payload = json.loads(out)  # would raise if anything else were printed
    assert payload["schema_version"] == 2
    assert payload["source"] == "account"
    assert payload["mode"] == "personal"
    assert payload["data_status"] == "available"
    assert payload["player_source"] == "opendota"


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
        assert json.loads(out)["schema_version"] == 2


def test_cache_json_reports_both_sources_and_total(tmp_path, capsys, fake_client):
    opendota_dir = tmp_path / "cache" / "opendota"
    stratz_dir = tmp_path / "cache" / "stratz"
    Cache(opendota_dir).set("open-1", {"x": 1})
    Cache(opendota_dir).set("open-2", {"x": 2})
    Cache(stratz_dir).set("stratz-1", {"x": 3})

    code, out, err = run(["--cache-dir", str(opendota_dir), "cache", "--json"], capsys)
    payload = json.loads(out)
    assert code == 0
    assert err == ""
    assert payload == {
        "schema_version": 2,
        "action": "inspect",
        "counts": {"opendota": 2, "stratz": 1, "total": 3},
        "directories": {"opendota": str(opendota_dir), "stratz": str(stratz_dir)},
    }


def test_cache_clear_covers_both_sources_but_preserves_foreign_files(tmp_path, capsys, fake_client):
    opendota_dir = tmp_path / "cache" / "opendota"
    stratz_dir = tmp_path / "cache" / "stratz"
    Cache(opendota_dir).set("open", {"x": 1})
    Cache(stratz_dir).set("stratz", {"x": 2})
    foreign_open = opendota_dir / "important.json"
    foreign_stratz = stratz_dir / "important.json"
    foreign_open.write_text('{"keep": true}', encoding="utf-8")
    foreign_stratz.write_text('{"keep": true}', encoding="utf-8")

    code, out, err = run(["--cache-dir", str(opendota_dir), "cache", "--clear", "--json"], capsys)
    payload = json.loads(out)
    assert code == 0
    assert err == ""
    assert payload["action"] == "clear"
    assert payload["counts"] == {"opendota": 1, "stratz": 1, "total": 2}
    assert foreign_open.exists()
    assert foreign_stratz.exists()
    assert Cache(opendota_dir).entries() == 0
    assert Cache(stratz_dir).entries() == 0


def test_errors_go_to_stderr_and_leave_stdout_empty(capsys, fake_client):
    code, out, err = run(["recommend"], capsys)
    assert code == 2
    assert out == ""
    assert "No account id" in err


def test_malformed_opendota_json_leaves_stdout_empty(capsys, fake_client):
    fake_client.hero_rows = [{"hero_id": 2, "games": 10, "win": 11}]
    code, out, err = run(["player", "--account-id", "123456789", "--json"], capsys)
    assert code == 2
    assert out == ""
    assert "hero count fields" in err
    assert "Traceback" not in err


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


def test_position_without_a_token_is_rejected_before_requests(capsys, fake_client, monkeypatch):
    monkeypatch.setattr(
        cli,
        "build_client",
        lambda args, settings: (_ for _ in ()).throw(AssertionError("client constructed")),
    )
    code, out, err = run(
        ["recommend", "--account-id", "123456789", "--position", "4", "--json"], capsys
    )
    assert code == 2
    assert out == ""
    assert "STRATZ_API_TOKEN" in err


def test_meta_stratz_without_a_bracket_is_rejected_before_requests(
    capsys, fake_client, monkeypatch
):
    monkeypatch.setattr(
        cli,
        "build_client",
        lambda args, settings: (_ for _ in ()).throw(AssertionError("client constructed")),
    )
    code, out, err = run(["meta", "--source", "stratz", "--json"], capsys)
    assert code == 2
    assert out == ""
    assert "--bracket is required" in err


def test_meta_explicit_stratz_requires_a_token_before_requests(capsys, fake_client):
    code, out, err = run(["meta", "--bracket", "5", "--source", "stratz", "--json"], capsys)

    assert code == 2
    assert out == ""
    assert "STRATZ_API_TOKEN" in err
    assert fake_client.calls == []


def test_meta_position_auto_without_a_bracket_is_rejected_before_requests(
    capsys, fake_client, monkeypatch
):
    from dotameta.config import Settings

    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(stratz_token="tok")))
    monkeypatch.setattr(
        cli,
        "build_client",
        lambda args, settings: (_ for _ in ()).throw(AssertionError("client constructed")),
    )
    code, out, err = run(["meta", "--position", "4", "--json"], capsys)
    assert code == 2
    assert out == ""
    assert "--bracket is required" in err


def test_meta_position_without_a_token_is_rejected_before_requests(
    capsys, fake_client, monkeypatch
):
    monkeypatch.setattr(
        cli,
        "build_client",
        lambda args, settings: (_ for _ in ()).throw(AssertionError("client constructed")),
    )
    code, out, err = run(["meta", "--bracket", "5", "--position", "4", "--json"], capsys)
    assert code == 2
    assert out == ""
    assert "STRATZ_API_TOKEN" in err


def test_position_with_explicit_opendota_is_rejected_before_requests(
    capsys, fake_client, monkeypatch
):
    monkeypatch.setattr(
        cli,
        "build_client",
        lambda args, settings: (_ for _ in ()).throw(AssertionError("client constructed")),
    )
    code, out, err = run(
        ["meta", "--bracket", "5", "--position", "4", "--source", "opendota", "--json"],
        capsys,
    )
    assert code == 2
    assert out == ""
    assert "requires Stratz" in err


def test_forcing_stratz_without_a_token_is_a_clear_error(capsys, fake_client):
    code, out, err = run(
        [
            "recommend",
            "--account-id",
            "123456789",
            "--bracket",
            "5",
            "--source",
            "stratz",
        ],
        capsys,
    )
    assert code == 2
    assert "STRATZ_API_TOKEN" in err
    assert out == ""


def test_auto_uses_stratz_for_immortal_when_a_token_exists(capsys, fake_client, monkeypatch):
    from dotameta.config import Settings

    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(stratz_token="tok")))
    fake_client.hero_rows = [{"hero_id": 2, "games": 20, "win": 10}]

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
    assert payload["player_source"] == "opendota"


def test_stratz_meta_rejects_nonempty_all_zero_counts(capsys, fake_client, monkeypatch):
    from dotameta.config import Settings

    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(stratz_token="tok")))

    class ZeroMetaStratz:
        def __init__(self, *args, **kwargs):
            pass

        def hero_win_rates(self, medal, position=None):
            return [{"heroId": 1, "matchCount": 0, "winCount": 0}]

    monkeypatch.setattr(cli, "StratzClient", ZeroMetaStratz)
    code, out, err = run(["meta", "--bracket", "5", "--source", "stratz", "--json"], capsys)
    assert code == 2
    assert out == ""
    assert "Stratz returned no usable hero data for medal 5" in err


def stratz_profile_payload(matches=100):
    return {
        "matchCount": matches,
        "isAnonymous": False,
        "allHistoryHeroesPerformance": [{"heroId": 2, "matchCount": matches, "winCount": 55}],
        "rankedAllPickHeroesPerformance": [{"heroId": 2, "matchCount": matches, "winCount": 55}],
    }


def test_explicit_stratz_uses_one_client_for_personal_and_meta(capsys, fake_client, monkeypatch):
    from dotameta.config import Settings

    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(stratz_token="tok")))
    instances = []

    class FakeStratz:
        def __init__(self, *args, **kwargs):
            instances.append(self)

        def player_hero_performance(self, account_id):
            return stratz_profile_payload()

        def hero_win_rates(self, medal, position=None):
            assert medal == 5
            return [{"heroId": 2, "matchCount": 50_000, "winCount": 26_000}]

    monkeypatch.setattr(cli, "StratzClient", FakeStratz)
    code, out, err = run(
        [
            "recommend",
            "--account-id",
            "123456789",
            "--bracket",
            "5",
            "--source",
            "stratz",
            "--json",
        ],
        capsys,
    )
    payload = json.loads(out)
    assert code == 0
    assert len(instances) == 1
    assert payload["player_source"] == "stratz"
    assert payload["meta_source"] == "stratz"
    assert payload["window_days"] is None
    assert payload["rank"] == "Unranked"


def test_auto_falls_back_to_stratz_only_for_unavailable_personal_data(
    capsys, fake_client, monkeypatch
):
    from dotameta.config import Settings

    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(stratz_token="tok")))

    class FakeStratz:
        def __init__(self, *args, **kwargs):
            pass

        def player_hero_performance(self, account_id):
            return stratz_profile_payload()

    monkeypatch.setattr(cli, "StratzClient", FakeStratz)
    code, out, err = run(["recommend", "--account-id", "123456789", "--json"], capsys)
    payload = json.loads(out)
    assert code == 0
    assert payload["player_source"] == "stratz"
    assert payload["meta_source"] == "opendota"
    assert payload["rank"] == "Legend 1"
    assert payload["window_days"] is None


def test_auto_does_not_construct_stratz_for_an_ordinary_public_profile(
    capsys, fake_client, monkeypatch
):
    from dotameta.config import Settings

    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(stratz_token="tok")))
    fake_client.hero_rows = [{"hero_id": 2, "games": 20, "win": 10}]

    class UnexpectedStratz:
        def __init__(self, *args, **kwargs):
            raise AssertionError("ordinary auto profile must stay on OpenDota")

    monkeypatch.setattr(cli, "StratzClient", UnexpectedStratz)
    code, out, err = run(["recommend", "--account-id", "123456789", "--json"], capsys)
    assert code == 0
    assert json.loads(out)["player_source"] == "opendota"


def test_explicit_opendota_never_falls_back_to_stratz(capsys, fake_client, monkeypatch):
    from dotameta.config import Settings

    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(stratz_token="tok")))

    class UnexpectedStratz:
        def __init__(self, *args, **kwargs):
            raise AssertionError("explicit OpenDota must not fall back")

    monkeypatch.setattr(cli, "StratzClient", UnexpectedStratz)
    code, out, err = run(
        [
            "recommend",
            "--account-id",
            "123456789",
            "--source",
            "opendota",
            "--json",
        ],
        capsys,
    )
    payload = json.loads(out)
    assert code == 0
    assert payload["player_source"] == "opendota"
    assert payload["data_status"] == "private_or_unavailable"


def test_player_command_accepts_explicit_stratz_and_reports_its_source(
    capsys, fake_client, monkeypatch
):
    from dotameta.config import Settings

    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(stratz_token="tok")))

    class FakeStratz:
        def __init__(self, *args, **kwargs):
            pass

        def player_hero_performance(self, account_id):
            return stratz_profile_payload()

    monkeypatch.setattr(cli, "StratzClient", FakeStratz)
    code, out, err = run(
        ["player", "--account-id", "123456789", "--source", "stratz", "--json"], capsys
    )
    payload = json.loads(out)
    assert code == 0
    assert payload["player_source"] == "stratz"
    assert payload["window_days"] is None
    assert payload["games_per_week"] is None


def test_player_explicit_stratz_requires_a_token_before_requests(capsys, fake_client):
    code, out, err = run(
        ["player", "--account-id", "123456789", "--source", "stratz", "--json"], capsys
    )

    assert code == 2
    assert out == ""
    assert "STRATZ_API_TOKEN" in err
    assert fake_client.calls == []


def test_human_player_output_explains_private_opendota_hero_data(capsys, fake_client):
    fake_client.hero_rows = []
    code, out, err = run(["player", "--account-id", "123456789"], capsys)
    assert code == 0
    assert "hero data    unavailable" in out
    assert "profile is private or OpenDota has no public hero" in out


def test_human_player_output_explains_an_empty_opendota_window(capsys, fake_client):
    fake_client.hero_rows = [{"hero_id": 2, "games": 0, "win": 0}]
    code, out, err = run(["player", "--account-id", "123456789"], capsys)
    assert code == 0
    assert "hero data    empty" in out
    assert "no ranked All Pick games are in this" in out


def test_human_player_output_explains_short_history_pace_suppression(capsys, fake_client):
    now = int(time.time())
    fake_client.matches = [{"hero_id": 2, "start_time": now - day * 24 * 3600} for day in range(4)]
    code, out, err = run(["player", "--account-id", "123456789", "--days", "7"], capsys)
    assert code == 0
    rendered = " ".join(out.split())
    assert "last 7 days" in rendered
    assert "pace unavailable" in rendered
    assert "requested history is only 7 days" in rendered
    assert "pace requires the full 30-day window" in rendered


def test_human_player_output_explains_an_empty_stratz_aggregate(capsys, fake_client, monkeypatch):
    from dotameta.config import Settings

    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(stratz_token="tok")))

    class EmptyStratz:
        def __init__(self, *args, **kwargs):
            pass

        def player_hero_performance(self, account_id):
            payload = stratz_profile_payload()
            payload["rankedAllPickHeroesPerformance"] = []
            return payload

    monkeypatch.setattr(cli, "StratzClient", EmptyStratz)
    code, out, err = run(["player", "--account-id", "123456789", "--source", "stratz"], capsys)
    assert code == 0
    assert "hero data    empty" in out
    assert "Stratz has no ranked-All-Pick aggregate rows" in out
    assert "this window" not in out


def test_json_warning_explains_an_empty_stratz_aggregate(capsys, fake_client, monkeypatch):
    from dotameta.config import Settings

    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(stratz_token="tok")))

    class EmptyStratz:
        def __init__(self, *args, **kwargs):
            pass

        def player_hero_performance(self, account_id):
            payload = stratz_profile_payload()
            payload["rankedAllPickHeroesPerformance"] = []
            return payload

        def hero_win_rates(self, medal, position=None):
            return [{"heroId": 2, "matchCount": 50_000, "winCount": 26_000}]

    monkeypatch.setattr(cli, "StratzClient", EmptyStratz)
    code, out, err = run(
        [
            "recommend",
            "--account-id",
            "123456789",
            "--bracket",
            "5",
            "--source",
            "stratz",
            "--json",
        ],
        capsys,
    )

    assert code == 0
    warning = next(item for item in json.loads(out)["warnings"] if "aggregate rows" in item)
    assert "ranked-All-Pick" in warning
    assert "window" not in warning


def test_stratz_profile_error_is_stable_and_json_stdout_stays_empty(
    capsys, fake_client, monkeypatch
):
    from dotameta.config import Settings
    from dotameta.stratz import StratzError

    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(stratz_token="tok")))

    class BrokenStratz:
        def __init__(self, *args, **kwargs):
            pass

        def player_hero_performance(self, account_id):
            raise StratzError("Stratz player payload was malformed")

    monkeypatch.setattr(cli, "StratzClient", BrokenStratz)
    code, out, err = run(
        ["player", "--account-id", "123456789", "--source", "stratz", "--json"], capsys
    )
    assert code == 2
    assert out == ""
    assert "payload was malformed" in err
    assert "Traceback" not in err


def test_explicit_stratz_unavailable_message_does_not_prescribe_opendota_privacy(
    capsys, fake_client, monkeypatch
):
    from dotameta.config import Settings

    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(stratz_token="tok")))

    class AnonymousStratz:
        def __init__(self, *args, **kwargs):
            pass

        def player_hero_performance(self, account_id):
            payload = stratz_profile_payload()
            payload["isAnonymous"] = True
            return payload

        def hero_win_rates(self, medal, position=None):
            return [{"heroId": 2, "matchCount": 50_000, "winCount": 26_000}]

    monkeypatch.setattr(cli, "StratzClient", AnonymousStratz)
    code, out, err = run(
        [
            "recommend",
            "--account-id",
            "123456789",
            "--bracket",
            "5",
            "--source",
            "stratz",
        ],
        capsys,
    )
    assert code == 0
    assert "Stratz aggregate is anonymous" in err
    assert "Stratz did not provide a complete public hero aggregate" in err
    assert "Expose Public Match Data" not in err


def test_stratz_fallback_unavailable_message_does_not_prescribe_opendota_privacy(
    capsys, fake_client, monkeypatch
):
    from dotameta.config import Settings

    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: Settings(stratz_token="tok")))

    class IncompleteStratz:
        def __init__(self, *args, **kwargs):
            pass

        def player_hero_performance(self, account_id):
            return {
                "matchCount": 100,
                "isAnonymous": False,
                "allHistoryHeroesPerformance": [{"heroId": 2, "matchCount": 10, "winCount": 5}],
                "rankedAllPickHeroesPerformance": [],
            }

    monkeypatch.setattr(cli, "StratzClient", IncompleteStratz)
    code, out, err = run(["recommend", "--account-id", "123456789"], capsys)
    assert code == 0
    assert "Stratz aggregate is anonymous" in err
    assert "Stratz did not provide a complete public hero aggregate" in err
    assert "Expose Public Match Data" not in err
