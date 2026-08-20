from __future__ import annotations

from dotameta.meta import build_meta
from dotameta.player import PlayerHero, PlayerProfile
from dotameta.recommend import (
    CATEGORY_DROP,
    CATEGORY_LEARN,
    CATEGORY_SPAM,
    recommend,
    spam_plan,
    uncertainty_discount,
)


def rank_of(recommendations, hero_id: int) -> int:
    return next(i for i, rec in enumerate(recommendations) if rec.hero_id == hero_id)


def test_a_long_personal_record_beats_a_four_game_hot_streak(hero_stats, profile):
    meta = build_meta(hero_stats, 5)
    results = recommend(profile, meta, min_bracket_picks=1000)
    # Hero 3: 60% over 200 games on a weak hero. Hero 2: 100% over 4 games.
    assert rank_of(results, 3) < rank_of(results, 2)


def test_unplayed_heroes_are_ranked_on_the_meta_alone(hero_stats, profile):
    results = recommend(profile, meta := build_meta(hero_stats, 5), min_bracket_picks=1000)
    unplayed = {rec.hero_id: rec for rec in results if rec.games == 0}
    assert unplayed[1].expected_winrate > unplayed[5].expected_winrate
    assert meta[1].winrate > meta[5].winrate


def test_thin_bracket_samples_are_excluded(hero_stats, profile):
    results = recommend(profile, build_meta(hero_stats, 5), min_bracket_picks=1000)
    assert all(rec.hero_id != 4 for rec in results)


def test_include_unplayed_can_be_turned_off(hero_stats, profile):
    results = recommend(
        profile, build_meta(hero_stats, 5), min_bracket_picks=1000, include_unplayed=False
    )
    assert {rec.hero_id for rec in results} == {2, 3}


def test_role_filter_applies(hero_stats, profile):
    results = recommend(profile, build_meta(hero_stats, 5), min_bracket_picks=1000, role="Support")
    assert [rec.hero_id for rec in results] == [5]


def test_categories(hero_stats):
    profile = PlayerProfile(
        account_id=1,
        name="Tester",
        rank_tier=51,
        games=200,
        wins=100,
        heroes={
            1: PlayerHero(hero_id=1, games=100, wins=60),  # good hero, good record
            3: PlayerHero(hero_id=3, games=100, wins=35),  # bad hero, bad record
        },
    )
    results = {rec.hero_id: rec for rec in recommend(profile, build_meta(hero_stats, 5))}
    assert results[1].category == CATEGORY_SPAM
    assert results[3].category == CATEGORY_DROP
    assert results[5].category == CATEGORY_LEARN  # never played


def test_uncertainty_discount_shrinks_as_games_accumulate():
    assert uncertainty_discount(0.6, 0) > uncertainty_discount(0.6, 50)
    assert uncertainty_discount(0.6, 50) > uncertainty_discount(0.6, 500)


def test_mmr_projection_is_symmetric_around_fifty_percent(hero_stats, profile):
    results = recommend(profile, build_meta(hero_stats, 5), min_bracket_picks=1000)
    for rec in results:
        expected = (2 * rec.expected_winrate - 1) * 100 * 25
        assert rec.mmr_per_100_games == expected
        if rec.expected_winrate > 0.5:
            assert rec.mmr_per_100_games > 0


def test_spam_plan_projects_weekly_mmr(hero_stats, profile):
    results = recommend(profile, build_meta(hero_stats, 5), min_bracket_picks=1000)
    plan = spam_plan(results, profile, pool_size=3)
    assert len(plan["pool"]) == 3
    assert plan["games_per_week"] == 10.0
    # 10 games a week at the pool's expected winrate, at 25 MMR a win.
    assert plan["mmr_per_week"] == (2 * plan["expected_winrate"] - 1) * 10.0 * 25


def test_spam_plan_handles_an_empty_board(profile):
    plan = spam_plan([], profile)
    assert plan["pool"] == []
    assert plan["mmr_per_week"] == 0.0
