from __future__ import annotations

import pytest

from dotameta.meta import build_meta
from dotameta.player import PlayerHero, PlayerProfile
from dotameta.recommend import (
    CATEGORY_DROP,
    CATEGORY_LEARN,
    CATEGORY_RISKY,
    CATEGORY_SPAM,
    mastery_of,
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
    assert plan.games_per_week == 10.0
    # Projected from the discounted winrate, not the optimistic one - this is the
    # number a user acts on, so it must not be inflated by thin samples.
    assert plan.mmr_per_week_low == (2 * plan.adjusted_winrate - 1) * 10.0 * 25
    assert plan.adjusted_winrate < plan.expected_winrate


def test_spam_plan_handles_an_empty_board(profile):
    plan = spam_plan([], profile)
    assert plan.pool == []
    assert not plan
    # No pool means no projection at all - not a projection of zero, and
    # certainly not a negative one computed from a defaulted 0% winrate.
    assert plan.mmr_per_week_low is None
    assert plan.mmr_per_100_low is None


def test_a_two_game_hero_is_not_advertised_as_a_good_bet(hero_stats):
    """Regression: the table used to print the optimistic projection.

    A hero played twice and won twice was shown at a large positive MMR/100 even
    though the model ranked it below a hero with 200 games, because the column
    displayed `expected_winrate` while sorting used `adjusted_winrate`.
    """
    profile = PlayerProfile(
        account_id=1,
        name="Veteran",
        rank_tier=71,
        games=2000,
        wins=1050,
        heroes={
            2: PlayerHero(hero_id=2, games=2, wins=2),
            3: PlayerHero(hero_id=3, games=200, wins=106),
        },
    )
    results = {rec.hero_id: rec for rec in recommend(profile, build_meta(hero_stats, 5))}
    lucky, grinded = results[2], results[3]

    assert lucky.mmr_per_100_games > 0  # the optimistic view still looks great
    assert lucky.mmr_per_100_games_conservative < 0  # the honest one does not
    assert lucky.mmr_per_100_games_conservative < grinded.mmr_per_100_games_conservative


def test_min_games_filters_thin_personal_samples(hero_stats, profile):
    # profile plays hero 2 four times and hero 3 two hundred times.
    kept = {
        rec.hero_id
        for rec in recommend(profile, build_meta(hero_stats, 5), min_games=20)
        if rec.games
    }
    assert kept == {3}


def test_min_games_still_allows_unplayed_heroes_through(hero_stats, profile):
    results = recommend(profile, build_meta(hero_stats, 5), min_games=20)
    assert any(rec.games == 0 for rec in results)


def test_the_two_cases_the_tool_exists_to_tell_apart(hero_stats):
    """1000 games at 53% on a strong hero vs 1 game on a strong hero.

    Deep experience on a hero that is merely fine beats a single game on a hero
    the bracket loves - the first is a climbing plan, the second is a coin flip.
    """
    profile = PlayerProfile(
        account_id=1,
        name="Spammer",
        rank_tier=51,
        games=1001,
        wins=531,
        heroes={
            2: PlayerHero(hero_id=2, games=1000, wins=530),  # average hero, mastered
            1: PlayerHero(hero_id=1, games=1, wins=1),  # strongest hero, one game
        },
    )
    results = recommend(profile, build_meta(hero_stats, 5), min_bracket_picks=1000)
    ranked = [rec.hero_id for rec in results]
    assert ranked.index(2) < ranked.index(1)

    mastered = next(rec for rec in results if rec.hero_id == 2)
    one_game = next(rec for rec in results if rec.hero_id == 1)
    assert mastered.category == CATEGORY_SPAM
    assert mastered.mastery == "mastered"
    assert one_game.category == CATEGORY_RISKY  # "worth a try", not "here is your plan"
    assert one_game.mastery == "thin"


def test_mastery_tiers():
    assert mastery_of(0) == "untested"
    assert mastery_of(5) == "thin"
    assert mastery_of(50) == "practiced"
    assert mastery_of(150) == "experienced"
    assert mastery_of(1000) == "mastered"


def test_edge_vs_meta_is_you_minus_the_bracket(hero_stats):
    profile = PlayerProfile(
        account_id=1,
        name="Tester",
        rank_tier=51,
        games=100,
        wins=60,
        heroes={3: PlayerHero(hero_id=3, games=100, wins=60)},
    )
    results = {rec.hero_id: rec for rec in recommend(profile, build_meta(hero_stats, 5))}
    # Hero 3 wins 45% in the bracket; this player wins 60% on it.
    assert results[3].edge_vs_meta == pytest.approx(0.15, abs=0.01)
    assert results[1].edge_vs_meta == 0.0  # never played, no edge to report


def test_a_thin_sample_on_a_weak_hero_is_still_only_risky(hero_stats):
    """15 games at 60% on a hero the bracket dislikes is not a 'drop'."""
    profile = PlayerProfile(
        account_id=1,
        name="Tester",
        rank_tier=51,
        games=15,
        wins=9,
        heroes={3: PlayerHero(hero_id=3, games=15, wins=9)},
    )
    results = {rec.hero_id: rec for rec in recommend(profile, build_meta(hero_stats, 5))}
    assert results[3].category == CATEGORY_RISKY


def test_a_negative_hero_never_enters_the_suggested_pool(hero_stats):
    """Regression: the pool was filled category-first, ignoring the projection.

    Categories were decided on the optimistic winrate while the printed MMR came
    from the discounted one, so the CLI could present a "climbing plan" whose own
    projection was negative.
    """
    profile = PlayerProfile(
        account_id=1,
        name="Marginal",
        rank_tier=51,
        games=120,
        wins=58,
        heroes={
            1: PlayerHero(hero_id=1, games=100, wins=56),  # genuinely positive
            3: PlayerHero(hero_id=3, games=20, wins=11),  # optimistic, not robust
        },
    )
    results = recommend(profile, build_meta(hero_stats, 5), min_bracket_picks=1000)
    plan = spam_plan(results, profile, pool_size=3)

    assert all(rec.adjusted_winrate > 0.5 for rec in plan.pool)
    assert plan.mmr_per_100_low is None or plan.mmr_per_100_low > 0


def test_the_pool_may_be_shorter_than_requested(hero_stats):
    """A short honest pool beats a padded one."""
    profile = PlayerProfile(
        account_id=1,
        name="One Trick",
        rank_tier=51,
        games=300,
        wins=175,
        heroes={1: PlayerHero(hero_id=1, games=300, wins=175)},
    )
    results = recommend(profile, build_meta(hero_stats, 5), min_bracket_picks=1000)
    plan = spam_plan(results, profile, pool_size=3)
    assert 0 < len(plan.pool) < 3


def test_a_player_with_nothing_positive_gets_no_pool_at_all(hero_stats):
    profile = PlayerProfile(
        account_id=1,
        name="Struggling",
        rank_tier=51,
        games=200,
        wins=70,
        heroes={
            1: PlayerHero(hero_id=1, games=100, wins=35),
            3: PlayerHero(hero_id=3, games=100, wins=35),
        },
    )
    results = recommend(
        profile, build_meta(hero_stats, 5), min_bracket_picks=1000, include_unplayed=False
    )
    plan = spam_plan(results, profile)
    assert plan.pool == []
    assert not plan


def test_category_never_contradicts_the_projection_shown(hero_stats, profile):
    """spam/keep must not sit next to a negative conservative MMR."""
    results = recommend(profile, build_meta(hero_stats, 5), min_bracket_picks=1000)
    for rec in results:
        if rec.category in ("spam", "keep"):
            assert rec.mmr_per_100_games_conservative > 0


def test_the_global_trend_does_not_reorder_the_ranking(hero_stats, profile):
    """A global signal must not silently outrank a bracket-specific one."""
    results = recommend(profile, build_meta(hero_stats, 5), min_bracket_picks=1000)
    keys = [rec.rank_key for rec in results]
    assert keys == sorted(keys, reverse=True)
    assert all(rec.rank_key == rec.adjusted_winrate for rec in results)


def test_a_winning_hero_is_never_labelled_drop(hero_stats):
    """A synthetic winning record may be risky, but it is not a loss."""
    profile = PlayerProfile(
        account_id=1,
        name="Tester",
        rank_tier=51,
        games=40,
        wins=23,
        heroes={2: PlayerHero(hero_id=2, games=40, wins=23)},
    )
    results = {rec.hero_id: rec for rec in recommend(profile, build_meta(hero_stats, 5))}
    assert results[2].expected_winrate > 0.5
    assert results[2].category != CATEGORY_DROP
    assert results[2].category == CATEGORY_RISKY

def test_an_unplayed_weak_hero_is_omitted(hero_stats):
    profile = PlayerProfile(
        account_id=1,
        name="Tester",
        rank_tier=51,
        games=0,
        wins=0,
        heroes={},
    )
    results = {rec.hero_id: rec for rec in recommend(profile, build_meta(hero_stats, 5))}

    assert 3 not in results
    assert all(rec.category not in (CATEGORY_RISKY, CATEGORY_DROP) for rec in results.values())
    assert results[1].category == CATEGORY_LEARN


def test_drop_still_means_losing(hero_stats):
    profile = PlayerProfile(
        account_id=1,
        name="Tester",
        rank_tier=51,
        games=100,
        wins=35,
        heroes={3: PlayerHero(hero_id=3, games=100, wins=35)},
    )
    results = {rec.hero_id: rec for rec in recommend(profile, build_meta(hero_stats, 5))}
    assert results[3].category == CATEGORY_DROP


def test_reasons_explain_the_discounted_ranking_and_conservative_mmr(hero_stats, profile):
    results = recommend(profile, build_meta(hero_stats, 5), min_bracket_picks=1000)

    for rec in results:
        reason = next(reason for reason in rec.reasons if reason.startswith("adjusted winrate"))
        assert reason == (
            f"adjusted winrate {rec.adjusted_winrate:.1%} after the heuristic uncertainty "
            f"discount; used for ranking and conservative MMR "
            f"{rec.mmr_per_100_games_conservative:+.0f} per 100 games"
        )
        assert "confidence interval" not in reason
