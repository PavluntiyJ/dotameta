from __future__ import annotations

import pytest

from dotameta.lanes import HeroLanes, LaneRecord, lane_of, lane_stats
from dotameta.opendota import OpenDotaError


def match(hero_id: int, lane_role: int | None, won: bool, roaming: bool = False) -> dict:
    return {
        "hero_id": hero_id,
        "lane_role": lane_role,
        "is_roaming": roaming,
        "player_slot": 0 if won else 128,
        "radiant_win": True,
    }


def is_win(m: dict) -> bool | None:
    if m.get("player_slot") is None or m.get("radiant_win") is None:
        return None
    return int(m["player_slot"]) < 128


def test_lane_labels():
    assert lane_of(match(1, 1, True)) == "safe"
    assert lane_of(match(1, 2, True)) == "mid"
    assert lane_of(match(1, 3, True)) == "off"
    assert lane_of(match(1, None, True)) is None
    assert lane_of(match(1, 1, True, roaming=True)) == "roam"


def test_lane_stats_counts_wins_per_lane():
    matches = [match(14, 3, True)] * 6 + [match(14, 2, False)] * 2
    stats = lane_stats(matches, is_win)
    assert stats[14].by_lane["off"] == LaneRecord(games=6, wins=6)
    assert stats[14].by_lane["mid"] == LaneRecord(games=2, wins=0)
    assert stats[14].main_lane == "off"


def test_unparsed_matches_are_skipped():
    stats = lane_stats([match(14, None, True)] * 10, is_win)
    assert stats == {}


def test_malformed_lane_values_are_stable_opendota_errors():
    with pytest.raises(OpenDotaError, match="match lane fields"):
        lane_of({"lane_role": "off"})


def test_main_lane_needs_a_minimum_sample():
    stats = lane_stats([match(14, 3, True)] * 2, is_win)
    assert stats[14].main_lane is None


def test_a_lane_winrate_requires_coverage_of_the_real_record():
    """A synthetic parsed subset cannot stand in for most of a hero record."""
    lanes = HeroLanes(hero_id=1, by_lane={"off": LaneRecord(games=16, wins=5)})
    lanes.played_games = 40
    assert lanes.coverage < 0.5
    assert lanes.reported_lane is None
    assert lanes.summary() == "off"


def test_a_well_covered_record_does_report_a_winrate():
    lanes = HeroLanes(hero_id=14, by_lane={"off": LaneRecord(games=40, wins=26)})
    lanes.played_games = 50
    assert lanes.reported_lane is not None
    assert lanes.summary() == "off 65% (40)"


def test_summary_is_empty_when_even_the_lane_is_unknown():
    assert HeroLanes(hero_id=14).summary() == ""


def test_a_rare_lane_never_outranks_the_main_one_on_raw_winrate():
    """Regression: best_lane took the maximum raw winrate across lanes.

    10/15 mid (66.7%) beat 60/100 safe (60%) and the tool announced mid as the
    better lane - a raw-winrate ranking with multiple-comparisons bias, in a
    project whose whole premise is not doing that.
    """
    lanes = HeroLanes(
        hero_id=14,
        by_lane={
            "safe": LaneRecord(games=100, wins=60),
            "mid": LaneRecord(games=15, wins=10),
        },
    )
    lanes.played_games = 115
    assert lanes.main_lane == "safe"
    assert lanes.summary().startswith("safe")
    assert "mid" not in lanes.summary()
