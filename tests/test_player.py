"""load_profile and pace, against a fake client. No network."""

from __future__ import annotations

from dotameta.player import (
    ALL_PICK_GAME_MODE,
    RANKED_LOBBY_TYPE,
    SECONDS_PER_DAY,
    DataStatus,
    games_per_week,
    load_profile,
)

NOW = 1_700_000_000.0


class FakeClient:
    """Records the filters each endpoint was called with."""

    def __init__(self, hero_rows=None, matches=None, wl=None, rank_tier=71):
        self.hero_rows = hero_rows if hero_rows is not None else []
        self.matches = matches if matches is not None else []
        self.wl = wl or {"win": 10, "lose": 10}
        self.rank_tier = rank_tier
        self.calls: dict[str, dict] = {}

    def player(self, account_id):
        return {"rank_tier": self.rank_tier, "profile": {"personaname": "Tester"}}

    def player_win_loss(self, account_id, **filters):
        self.calls["wl"] = filters
        return self.wl

    def player_heroes(self, account_id, **filters):
        self.calls["heroes"] = filters
        return self.hero_rows

    def player_matches(self, account_id, limit=100, **filters):
        self.calls["matches"] = dict(filters, limit=limit)
        return self.matches


def match(days_ago: float, hero_id=14, won=True, lobby=RANKED_LOBBY_TYPE, mode=ALL_PICK_GAME_MODE):
    return {
        "start_time": int(NOW - days_ago * SECONDS_PER_DAY),
        "hero_id": hero_id,
        "lane_role": 3,
        "is_roaming": False,
        "player_slot": 0 if won else 128,
        "radiant_win": True,
        "lobby_type": lobby,
        "game_mode": mode,
    }


# -- P0.1: ranked filters --------------------------------------------------
def test_every_personal_endpoint_uses_the_same_ranked_filters():
    client = FakeClient()
    load_profile(client, 1, recent_days=90, now=NOW)
    expected = {
        "lobby_type": RANKED_LOBBY_TYPE,
        "game_mode": ALL_PICK_GAME_MODE,
        "date": 90,
    }
    assert client.calls["wl"] == expected
    assert client.calls["heroes"] == expected
    # /matches carries the same filters plus paging and the field projection.
    matches_call = client.calls["matches"]
    assert {key: matches_call[key] for key in expected} == expected
    assert matches_call["limit"] == 200


def test_match_projection_requests_every_field_the_code_reads():
    client = FakeClient()
    load_profile(client, 1, now=NOW)
    projected = (
        set(client.calls["matches"]["project"]) if "project" in client.calls["matches"] else set()
    )
    # `project` replaces defaults, so start_time and the win fields must be asked for.
    assert {"start_time", "player_slot", "radiant_win", "lane_role"} <= projected


def test_unranked_rows_that_slip_through_are_dropped_locally():
    matches = [match(1)] * 5 + [match(1, lobby=0)] * 5 + [match(1, mode=23)] * 5
    client = FakeClient(matches=matches)
    profile = load_profile(client, 1, now=NOW)
    # Only the five ranked All Pick rows reach the lane split.
    assert profile.lanes[14].total_games == 5


# -- P0.3: pace ------------------------------------------------------------
def test_two_games_an_hour_apart_are_not_336_games_a_week():
    """Regression: len(matches)/(last-first) produced exactly that."""
    matches = [match(0.0), match(1 / 24)]
    pace, note = games_per_week(matches, now=NOW)
    assert pace is None
    assert "2 ranked game" in note


def test_pace_uses_a_trailing_window_ending_now():
    matches = [match(day) for day in range(0, 30, 2)]  # 15 games in 30 days
    pace, note = games_per_week(matches, now=NOW)
    assert pace == 15 * 7 / 30
    assert note == ""


def test_a_burst_long_ago_is_not_a_current_pace():
    """Regression: a heavy month two years back read as today's pace."""
    matches = [match(700 + day) for day in range(30)]
    pace, note = games_per_week(matches, now=NOW)
    assert pace is None
    assert "last played" in note


def test_a_truncated_history_refuses_to_quote_a_pace():
    matches = [match(day / 24) for day in range(10)]  # all within one window
    pace, note = games_per_week(matches, now=NOW, sample_limit=10)
    assert pace is None
    assert "truncated" in note


def test_no_matches_at_all():
    pace, note = games_per_week([], now=NOW)
    assert pace is None and note


# -- P1.8: data status -----------------------------------------------------
def test_a_public_but_idle_account_is_not_called_private():
    """Regression: told an inactive public player to change a privacy setting."""
    client = FakeClient(hero_rows=[{"hero_id": 14, "games": 0, "win": 0}])
    profile = load_profile(client, 1, now=NOW)
    assert profile.data_status is DataStatus.EMPTY_WINDOW


def test_no_rows_at_all_reads_as_private_or_unavailable():
    profile = load_profile(FakeClient(hero_rows=[]), 1, now=NOW)
    assert profile.data_status is DataStatus.PRIVATE_OR_UNAVAILABLE


def test_rows_with_games_read_as_available():
    client = FakeClient(hero_rows=[{"hero_id": 14, "games": 30, "win": 18}])
    profile = load_profile(client, 1, now=NOW)
    assert profile.data_status is DataStatus.AVAILABLE
    assert profile.hero(14).games == 30


# -- P1.9: unknown outcomes ------------------------------------------------
def test_an_undecidable_match_is_excluded_from_both_wins_and_games():
    unknown = match(1)
    del unknown["radiant_win"]
    client = FakeClient(matches=[match(1, won=True), match(1, won=False), unknown])
    profile = load_profile(client, 1, now=NOW)
    assert profile.recent_games == 2  # not 3
    assert profile.recent_wins == 1
    assert profile.lanes[14].by_lane["off"].games == 2
