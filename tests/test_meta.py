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


def test_relative_pick_frequency_is_against_an_average_hero(hero_stats):
    meta = build_meta(hero_stats, 5)
    assert meta[1].relative_pick_frequency > 1.0
    assert meta[4].relative_pick_frequency < 0.1


def test_baseline_sits_near_fifty_percent(hero_stats):
    assert 0.45 < baseline_winrate(build_meta(hero_stats, 5)) < 0.55


def test_role_filter(hero_stats):
    meta = build_meta(hero_stats, 5)
    names = [entry.name for entry in top_meta_heroes(meta, limit=10, role="support")]
    assert names == ["Support Hero"]


def test_winrate_trend_detects_a_rising_hero():
    # Seven bins, as the live payload ships: three older, three recent, and a
    # partial current one that must be discarded rather than compared.
    rising = hero_row(
        9,
        "Rising",
        picks=10_000,
        winrate=0.5,
        trend=(
            [1000, 1000, 1000, 1000, 1000, 1000, 120],
            [480, 480, 480, 550, 550, 550, 12],
        ),
    )
    assert winrate_trend(rising) > 0.05


def test_winrate_trend_discards_the_partial_final_bin():
    """The last bin is a fraction of a period and would drag the trend down."""
    flat = hero_row(
        9,
        "Flat",
        picks=10_000,
        winrate=0.5,
        trend=(
            [1000, 1000, 1000, 1000, 1000, 1000, 50],
            [500, 500, 500, 500, 500, 500, 5],
        ),
    )
    assert winrate_trend(flat) == 0.0


def test_winrate_trend_is_zero_without_enough_data():
    assert winrate_trend({"pub_pick_trend": [10], "pub_win_trend": [5]}) == 0.0
    assert winrate_trend({}) == 0.0


def test_resolve_bracket_picks_the_mathematically_nearest_populated_medal():
    """Regression: it scanned every lower medal before ever looking up.

    An empty Archon (4) with a populated Legend (5) one step above answered
    Herald (1) instead.
    """
    rows = [hero_row(1, "Only In Legend", picks=50_000, winrate=0.5, medal=5)]
    assert resolve_bracket(rows, 4) == 5
    assert resolve_bracket(rows, 5) == 5
    assert resolve_bracket(rows, 8) == 5


def test_resolve_bracket_breaks_ties_downward():
    rows = [
        hero_row(1, "Low", picks=10_000, winrate=0.5, medal=3),
        hero_row(2, "High", picks=10_000, winrate=0.5, medal=5),
    ]
    # Medal 4 is equidistant from 3 and 5; the lower bracket is the safer answer.
    assert resolve_bracket(rows, 4) == 3


def test_thin_sample_heroes_never_reach_the_strongest_table(hero_stats):
    """Even with a huge limit, a 30-pick hero is not one of the strongest."""
    meta = build_meta(hero_stats, 5)
    names = [entry.name for entry in top_meta_heroes(meta, limit=100)]
    assert "Tiny Sample Hero" not in names


def test_stratz_rows_produce_the_same_shape_as_opendota_rows(hero_stats):
    """Both sources must go through one aggregation, or they drift apart."""
    from dotameta.meta import build_meta_from_stratz, hero_info_from_opendota

    heroes = [
        {"id": 1, "localized_name": "Strong Meta Hero", "primary_attr": "str", "roles": ["Carry"]},
        {"id": 2, "localized_name": "Average Hero", "primary_attr": "str", "roles": ["Carry"]},
    ]
    rows = [
        {"heroId": 1, "matchCount": 100_000, "winCount": 55_000},
        {"heroId": 2, "matchCount": 100_000, "winCount": 50_000},
    ]
    meta = build_meta_from_stratz(rows, hero_info_from_opendota(heroes))

    assert meta[1].name == "Strong Meta Hero"
    assert meta[1].winrate == 0.55
    assert meta[1].roles == ("Carry",)
    # Same aggregation as the OpenDota path: baseline near 50%, pick share equal.
    assert meta[1].relative_pick_frequency == meta[2].relative_pick_frequency
    assert 0.52 < meta[1].delta_vs_baseline + 0.5 < 0.58


def test_stratz_meta_carries_no_invented_trend():
    """The OpenDota trend arrays are global; they do not describe a Stratz bracket."""
    from dotameta.meta import build_meta_from_stratz

    meta = build_meta_from_stratz([{"heroId": 1, "matchCount": 5000, "winCount": 2600}], {})
    assert meta[1].trend == 0.0


def test_stratz_rows_without_a_hero_id_are_skipped():
    from dotameta.meta import build_meta_from_stratz

    meta = build_meta_from_stratz([{"matchCount": 10, "winCount": 5}], {})
    assert meta == {}
