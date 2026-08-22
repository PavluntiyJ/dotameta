from __future__ import annotations

import argparse

import pytest

from dotameta.cli import STEAM64_BASE, build_client, build_parser, parse_account_id
from dotameta.config import Settings
from dotameta.constants import format_rank_tier, medal_from_rank_tier
from dotameta.player import match_outcome
from dotameta.stats import shrink_to_prior, wilson_lower_bound, winrate

SYNTHETIC_ACCOUNT_ID = 123456789
SYNTHETIC_STEAM64_ID = 76561198083722517


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


def test_steam64_ids_are_converted_to_account_ids():
    assert parse_account_id(str(SYNTHETIC_ACCOUNT_ID)) == SYNTHETIC_ACCOUNT_ID
    assert STEAM64_BASE + SYNTHETIC_ACCOUNT_ID == SYNTHETIC_STEAM64_ID
    assert parse_account_id(str(SYNTHETIC_STEAM64_ID)) == SYNTHETIC_ACCOUNT_ID


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
    assert parse_account_id(pasted) == SYNTHETIC_ACCOUNT_ID


def test_steam_community_links_are_converted_too():
    url = f"https://steamcommunity.com/profiles/{SYNTHETIC_STEAM64_ID}/"
    assert parse_account_id(url) == SYNTHETIC_ACCOUNT_ID


def test_a_link_without_digits_is_rejected():
    with pytest.raises(argparse.ArgumentTypeError):
        parse_account_id("https://www.dotabuff.com/players/not-a-number")
    with pytest.raises(argparse.ArgumentTypeError):
        parse_account_id("nonsense")


def test_match_outcome_maps_player_slot_to_side():
    assert match_outcome({"player_slot": 0, "radiant_win": True}) is True
    assert match_outcome({"player_slot": 128, "radiant_win": True}) is False
    assert match_outcome({"player_slot": 129, "radiant_win": False}) is True


def test_an_undecidable_match_is_not_a_loss():
    """Regression: a row missing either field used to count as a loss."""
    assert match_outcome({}) is None
    assert match_outcome({"player_slot": 0}) is None
    assert match_outcome({"radiant_win": True}) is None


def test_days_zero_means_all_history():
    args = build_parser().parse_args(["recommend", "--days", "0"])
    assert args.days == 0  # normalised to None by main()


def test_parser_requires_a_subcommand():
    with pytest.raises(SystemExit):
        build_parser().parse_args([])


def test_api_key_is_not_accepted_on_the_command_line():
    parser = build_parser()
    assert "--api-key" not in parser.format_help()
    with pytest.raises(SystemExit):
        parser.parse_args(["--api-key", "secret", "meta"])


def test_environment_api_key_still_reaches_the_opendota_client(tmp_path):
    args = build_parser().parse_args(["--cache-dir", str(tmp_path), "meta"])
    client = build_client(args, Settings(api_key="environment-secret"))
    assert client.api_key == "environment-secret"
