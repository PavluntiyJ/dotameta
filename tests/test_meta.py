from __future__ import annotations

from dotameta.meta import (
    baseline_winrate,
    bracket_counts,
    build_meta,
    resolve_bracket,
    top_meta_heroes,
    winrate_trend,
)
from factories import hero_row


def test_bracket_counts_reads_the_requested_medal(hero_stats):
    assert bracket_counts(hero_stats[0], 5) == (100_000, 55_000)
    assert bracket_counts(hero_stats[0], 1) == (0, 0)


def test_bracket_counts_none_medal_uses_all_public_matches(hero_stats):
    assert bracket_counts(hero_stats[0], None) == (100_000, 55_000)


def test_resolve_bracket_falls_back_to_a_populated_medal(hero_stats):
    # Immortal (8) is empty in real payloads; we should land on 5, not on nothing.
    assert resolve_bracket(hero_stats, 8) == 5
    assert resolve_bracket(hero_stats, 5) == 5
    assert resolve_bracket(hero_stats, None) is None


def test_build_meta_ranks_by_sample_aware_winrate(hero_stats):
    meta = build_meta(hero_stats, 5)
    ranked = [entry.name for entry in top_meta_heroes(meta, limit=3)]
    assert ranked[0] == "Strong Meta Hero"
    # 90% over 30 games must not beat 55% over 100k.
    assert "Tiny Sample Hero" not in ranked


def test_small_samples_get_no_lower_bound(hero_stats):
    meta = build_meta(hero_stats, 5, min_picks=200)
    assert meta[4].winrate == 0.9  # raw ratio is still reported...
    assert meta[4].winrate_lb == 0.0  # ...but it cannot be ranked on


def test_contest_rate_is_relative_to_an_average_hero(hero_stats):
    meta = build_meta(hero_stats, 5)
    assert meta[1].contest_rate > 1.0
    assert meta[4].contest_rate < 0.1


def test_baseline_sits_near_fifty_percent(hero_stats):
    assert 0.45 < baseline_winrate(build_meta(hero_stats, 5)) < 0.55


def test_role_filter(hero_stats):
    meta = build_meta(hero_stats, 5)
    names = [entry.name for entry in top_meta_heroes(meta, limit=10, role="support")]
    assert names == ["Support Hero"]


def test_winrate_trend_detects_a_rising_hero():
    rising = hero_row(
        9,
        "Rising",
        picks=10_000,
        winrate=0.5,
        trend=([1000, 1000, 1000, 1000], [480, 480, 550, 550]),
    )
    assert winrate_trend(rising) > 0.05


def test_winrate_trend_is_zero_without_enough_data():
    assert winrate_trend({"pub_pick_trend": [10], "pub_win_trend": [5]}) == 0.0
    assert winrate_trend({}) == 0.0
