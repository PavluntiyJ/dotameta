from __future__ import annotations

import pytest

from dotameta.paste import parse_hero_list

HEROES = [
    {"id": 14, "localized_name": "Pudge", "name": "npc_dota_hero_pudge"},
    {"id": 11, "localized_name": "Shadow Fiend", "name": "npc_dota_hero_nevermore"},
    {"id": 41, "localized_name": "Faceless Void", "name": "npc_dota_hero_faceless_void"},
    {"id": 1, "localized_name": "Anti-Mage", "name": "npc_dota_hero_antimage"},
    {"id": 53, "localized_name": "Nature's Prophet", "name": "npc_dota_hero_furion"},
    {"id": 21, "localized_name": "Windranger", "name": "npc_dota_hero_windrunner"},
    {"id": 8, "localized_name": "Juggernaut", "name": "npc_dota_hero_juggernaut"},
]


def by_name(result, name):
    return next(hero for hero in result.heroes if hero.name == name)


def test_name_with_games_and_percentage():
    result = parse_hero_list("Pudge 1043 53%", HEROES)
    hero = by_name(result, "Pudge")
    assert (hero.games, hero.wins) == (1043, 553)


def test_thousands_separators_and_trailing_columns():
    result = parse_hero_list("Nature's Prophet\t1,204\t49.9%\t7:32", HEROES)
    hero = by_name(result, "Nature's Prophet")
    # 7:32 is a match duration and must not be read as a count.
    assert hero.games == 1204
    assert hero.wins == 601


def test_games_then_wins_without_a_percentage():
    result = parse_hero_list("Juggernaut 250 130", HEROES)
    hero = by_name(result, "Juggernaut")
    assert (hero.games, hero.wins) == (250, 130)


def test_comma_separated_and_old_hero_names():
    result = parse_hero_list("Nevermore, 88, 61.4%", HEROES)
    hero = by_name(result, "Shadow Fiend")
    assert hero.games == 88


def test_short_aliases():
    result = parse_hero_list("void 30 50%\nwr 10 40%", HEROES)
    assert {hero.name for hero in result.heroes} == {"Faceless Void", "Windranger"}


def test_bare_name_counts_as_zero_games():
    result = parse_hero_list("Anti-Mage", HEROES)
    assert by_name(result, "Anti-Mage").games == 0


@pytest.mark.parametrize("line", ["Pudge 100", "Pudge 53%"])
def test_incomplete_statistical_rows_are_unmatched(line):
    result = parse_hero_list(line, HEROES)

    assert result.heroes == []
    assert result.unmatched == [line]


def test_unrecognised_lines_are_reported_not_dropped_silently():
    result = parse_hero_list("Pudge 10 50%\ntotal 999 games\n", HEROES)
    assert [hero.name for hero in result.heroes] == ["Pudge"]
    assert result.unmatched == ["total 999 games"]


@pytest.mark.parametrize(
    "line",
    [
        "Pudge -10 50%",
        "Pudge 10 -50%",
        "Pudge 10 -1",
        "Pudge 10.5 50%",
        "Pudge 10 5.5",
        "Pudge 10 150%",
        "Pudge 10 11",
    ],
)
def test_invalid_statistics_are_reported_as_unmatched(line):
    result = parse_hero_list(line, HEROES)
    assert result.heroes == []
    assert result.unmatched == [line]


def test_duplicate_lines_keep_the_larger_sample():
    result = parse_hero_list("Pudge 10 50%\nPudge 900 55%", HEROES)
    assert by_name(result, "Pudge").games == 900


def test_total_games_across_the_paste():
    result = parse_hero_list("Pudge 100 50%\nJuggernaut 50 50%", HEROES)
    assert result.total_games == 150
