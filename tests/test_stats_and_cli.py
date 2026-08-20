from __future__ import annotations

import argparse

import pytest

from dotameta.cli import STEAM64_BASE, build_parser, parse_account_id
from dotameta.constants import format_rank_tier, medal_from_rank_tier, parse_bracket
from dotameta.player import is_win
from dotameta.stats import shrink_to_prior, wilson_lower_bound, winrate


def test_winrate_handles_an_empty_sample():
    assert winrate(0, 0) == 0.0
    assert winrate(3, 4) == 0.75


def test_wilson_punishes_small_samples():
    assert wilson_lower_bound(3, 3) < wilson_lower_bound(550, 1000)
    assert wilson_lower_bound(0, 0) == 0.0


def test_shrinkage_moves_from_prior_to_observation():
    prior = 0.5
    few = shrink_to_prior(4, 4, prior, strength=25)
    many = shrink_to_prior(400, 400, prior, strength=25)
    assert prior < few < many
    assert shrink_to_prior(0, 0, prior) == prior


def test_rank_tier_parsing():
    assert medal_from_rank_tier(74) == 7
    assert medal_from_rank_tier(None) is None
    assert format_rank_tier(74) == "Divine 4"
    assert format_rank_tier(None) == "Unranked"


def test_bracket_parsing_accepts_names_and_numbers():
    assert parse_bracket("divine") == 7
    assert parse_bracket("7") == 7
    assert parse_bracket(7) == 7
    assert parse_bracket("not a rank") is None
    assert parse_bracket("99") is None


def test_steam64_ids_are_converted_to_account_ids():
    assert parse_account_id("123456789") == 123456789
    assert parse_account_id(str(STEAM64_BASE + 123456789)) == 123456789


@pytest.mark.parametrize(
    "pasted",
    [
        "123456789",
        "https://www.opendota.com/players/123456789",
        "https://www.dotabuff.com/players/123456789",
        "https://www.dotabuff.com/players/123456789/matches",
        "https://stratz.com/players/123456789?tab=overview",
        "opendota.com/players/123456789/heroes",
        "  https://www.opendota.com/players/123456789/  ",
    ],
)
def test_profile_links_resolve_to_the_same_account(pasted):
    # Users paste a profile link, not a 32-bit account id.
    assert parse_account_id(pasted) == 123456789


def test_steam_community_links_are_converted_too():
    assert parse_account_id("https://steamcommunity.com/profiles/76561198083722517/") == 123456789


def test_a_link_without_digits_is_rejected():
    with pytest.raises(argparse.ArgumentTypeError):
        parse_account_id("https://www.dotabuff.com/players/mrbeast")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_account_id("nonsense")


def test_is_win_maps_player_slot_to_side():
    assert is_win({"player_slot": 0, "radiant_win": True}) is True
    assert is_win({"player_slot": 128, "radiant_win": True}) is False
    assert is_win({"player_slot": 129, "radiant_win": False}) is True
    assert is_win({}) is False


def test_days_zero_means_all_history():
    args = build_parser().parse_args(["recommend", "--days", "0"])
    assert args.days == 0  # normalised to None by main()


def test_parser_requires_a_subcommand():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])
