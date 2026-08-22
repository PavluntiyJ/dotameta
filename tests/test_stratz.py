"""Stratz transport tests. Offline: the session is a fake.

These pin the *contract* - auth, retries, GraphQL error handling, caching - not
the schema. Whether `winWeek` still exists upstream is what `verify_access()` is
for; a unit test cannot know, and pretending otherwise would be theatre.
"""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from email.utils import format_datetime

import pytest
import requests

from dotameta.stratz import MAX_ATTEMPTS, USER_AGENT, StratzClient, StratzError


class FakeResponse:
    def __init__(self, status=200, body=None, text="", headers=None, bad_json=False):
        self.status_code = status
        self._body = body if body is not None else {"data": {}}
        self.text = text
        self.headers = headers or {}
        self._bad_json = bad_json

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self):
        if self._bad_json:
            raise json.JSONDecodeError("no", "", 0)
        return self._body


class FakeSession:
    def __init__(self, script):
        self.script = list(script)
        self.posts = []
        self.headers = {}

    def post(self, url, json=None, timeout=None):
        self.posts.append({"url": url, "json": json, "timeout": timeout})
        item = self.script.pop(0) if self.script else FakeResponse()
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def slept():
    return []


def make_client(tmp_path, script, slept, token="tok", **kwargs):
    return StratzClient(
        token=token,
        cache_dir=tmp_path / "cache",
        session=FakeSession(script),
        sleep=slept.append,
        **kwargs,
    )


def test_a_token_is_required():
    with pytest.raises(StratzError, match="token is required"):
        StratzClient(token="")


def test_requests_carry_the_headers_cloudflare_expects(tmp_path, slept):
    client = make_client(tmp_path, [FakeResponse(body={"data": {"x": 1}})], slept)
    client.query("{ x }")
    # A browser-like User-Agent gets challenged; this exact value does not.
    assert client.session.headers["User-Agent"] == USER_AGENT
    assert client.session.headers["Authorization"] == "Bearer tok"


def test_a_graphql_error_inside_a_200_is_not_success(tmp_path, slept):
    """Regression risk: GraphQL reports failures with HTTP 200.

    Checking only the status code would read a broken query as an empty result.
    """
    body = {"errors": [{"message": "Cannot query field 'winWeek'"}], "data": None}
    client = make_client(tmp_path, [FakeResponse(body=body)], slept)
    with pytest.raises(StratzError, match="winWeek"):
        client.query("{ heroStats { winWeek { heroId } } }")


def test_a_response_without_a_data_object_is_an_error(tmp_path, slept):
    client = make_client(tmp_path, [FakeResponse(body={"data": None})], slept)
    with pytest.raises(StratzError, match="no data object"):
        client.query("{ x }")


def test_a_bad_token_is_not_retried(tmp_path, slept):
    client = make_client(tmp_path, [FakeResponse(status=403)], slept)
    with pytest.raises(StratzError, match="STRATZ_API_TOKEN"):
        client.query("{ x }")
    assert slept == []


def test_timeouts_are_retried_then_reported(tmp_path, slept):
    client = make_client(tmp_path, [requests.Timeout()] * MAX_ATTEMPTS, slept)
    with pytest.raises(StratzError, match="timed out"):
        client.query("{ x }")
    assert len(slept) == MAX_ATTEMPTS - 1


def test_rate_limiting_honours_retry_after(tmp_path, slept):
    script = [
        FakeResponse(status=429, headers={"Retry-After": "5"}),
        FakeResponse(body={"data": {"x": 1}}),
    ]
    client = make_client(tmp_path, script, slept)
    assert client.query("{ x }") == {"x": 1}
    assert 5 in slept


@pytest.mark.parametrize("status", [429, 500, 503])
@pytest.mark.parametrize("delay", [12, 300])
def test_retry_after_http_dates_apply_to_rate_limits_and_server_errors(
    tmp_path, slept, status, delay
):
    now = 1_700_000_000
    header = format_datetime(datetime.fromtimestamp(now + delay, UTC), usegmt=True)
    script = [
        FakeResponse(status=status, headers={"Retry-After": header}),
        FakeResponse(body={"data": {"x": 1}}),
    ]
    client = make_client(tmp_path, script, slept, wall_clock=lambda: now)

    assert client.query("{ x }") == {"x": 1}
    assert slept == [min(delay, 30)]


@pytest.mark.parametrize(
    ("header", "expected"), [("9", 9), ("0" * 5_000 + "9", 9), ("9" * 5_000, 30)]
)
def test_retry_after_delta_seconds_apply_to_server_errors(tmp_path, slept, header, expected):
    script = [
        FakeResponse(status=503, headers={"Retry-After": header}),
        FakeResponse(body={"data": {"x": 1}}),
    ]
    client = make_client(tmp_path, script, slept)

    assert client.query("{ x }") == {"x": 1}
    assert slept == [expected]


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize(
    "header", ["soon", "-1", "nan", "inf", "-inf", "Tue, 14 Nov 2023 22:13:19 GMT"]
)
def test_invalid_or_past_retry_after_falls_back_to_safe_backoff(tmp_path, slept, status, header):
    script = [
        FakeResponse(status=status, headers={"Retry-After": header}),
        FakeResponse(body={"data": {"x": 1}}),
    ]
    client = make_client(tmp_path, script, slept, wall_clock=lambda: 1_700_000_000)
    assert client.query("{ x }") == {"x": 1}
    assert slept and all(math.isfinite(delay) and delay >= 0 for delay in slept)


def test_a_200_that_is_not_json_is_an_error(tmp_path, slept):
    client = make_client(tmp_path, [FakeResponse(bad_json=True)], slept)
    with pytest.raises(StratzError, match="not JSON"):
        client.query("{ x }")


def test_results_are_cached(tmp_path, slept):
    client = make_client(tmp_path, [FakeResponse(body={"data": {"x": 1}})], slept)
    client.query("{ x }")
    client.query("{ x }")
    assert client.calls_made == 1


def test_tokens_with_different_permissions_never_share_cache_entries(tmp_path, slept):
    first = make_client(
        tmp_path, [FakeResponse(body={"data": {"viewer": "first"}})], slept, token="token-one"
    )
    second = make_client(
        tmp_path, [FakeResponse(body={"data": {"viewer": "second"}})], slept, token="token-two"
    )

    assert first.query("{ viewer }") == {"viewer": "first"}
    assert second.query("{ viewer }") == {"viewer": "second"}
    assert first.calls_made == second.calls_made == 1

    first_cached = make_client(tmp_path, [], slept, token="token-one")
    second_cached = make_client(tmp_path, [], slept, token="token-two")
    assert first_cached.query("{ viewer }") == {"viewer": "first"}
    assert second_cached.query("{ viewer }") == {"viewer": "second"}
    assert first_cached.calls_made == second_cached.calls_made == 0

    entries = list((tmp_path / "cache").glob("*.json"))
    assert len(entries) == 2
    for entry in entries:
        contents = entry.read_text(encoding="utf-8")
        assert "token-one" not in contents
        assert "token-two" not in contents
        assert "token-one" not in entry.name
        assert "token-two" not in entry.name


def test_the_token_never_reaches_the_cache(tmp_path, slept):
    secret = "super-secret-token"
    client = make_client(tmp_path, [FakeResponse(body={"data": {"x": 1}})], slept, token=secret)
    client.query("{ x }")
    for entry in (tmp_path / "cache").glob("*.json"):
        assert secret not in entry.read_text(encoding="utf-8")


def test_a_non_success_error_does_not_reflect_the_response_body(tmp_path, slept):
    secret = "private=request-context"
    client = make_client(tmp_path, [FakeResponse(status=418, text=secret)], slept)
    with pytest.raises(StratzError) as caught:
        client.query("{ x }")
    assert str(caught.value) == "Stratz request failed (HTTP 418)"
    assert secret not in str(caught.value)


def test_hero_win_rates_rejects_an_unknown_medal(tmp_path, slept):
    client = make_client(tmp_path, [], slept)
    with pytest.raises(StratzError, match="no Stratz bracket"):
        client.hero_win_rates(medal=99)


def test_hero_win_rates_asks_for_immortal_and_a_position(tmp_path, slept):
    """The whole point of this source: a bracket OpenDota does not publish."""
    body = {"data": {"heroStats": {"winWeek": [{"heroId": 14, "matchCount": 9, "winCount": 5}]}}}
    client = make_client(tmp_path, [FakeResponse(body=body)], slept)
    rows = client.hero_win_rates(medal=8, position=4)
    assert rows == [{"heroId": 14, "matchCount": 9, "winCount": 5}]

    sent = client.session.posts[0]["json"]["query"]
    assert "IMMORTAL" in sent
    assert "POSITION_4" in sent
    assert "ALL_PICK_RANKED" in sent


@pytest.mark.parametrize(
    "win_week",
    [
        None,
        [None],
        [{"heroId": 14, "matchCount": 2, "winCount": 3}],
        [{"heroId": 14, "matchCount": "2", "winCount": 1}],
    ],
)
def test_malformed_meta_payload_is_rejected_before_caching(tmp_path, slept, win_week):
    body = {"data": {"heroStats": {"winWeek": win_week}}}
    client = make_client(tmp_path, [FakeResponse(body=body)], slept)

    with pytest.raises(StratzError):
        client.hero_win_rates(medal=8)

    assert client.cache.entries() == 0


def test_player_hero_performance_uses_the_verified_query_shape(tmp_path, slept):
    synthetic_account_id = 123456789
    body = {
        "data": {
            "player": {
                "matchCount": 120,
                "steamAccount": {"isAnonymous": False},
                "allHistoryHeroesPerformance": [{"heroId": 14, "matchCount": 120, "winCount": 65}],
                "rankedAllPickHeroesPerformance": [
                    {"heroId": 14, "matchCount": 40, "winCount": 23}
                ],
            }
        }
    }
    client = make_client(tmp_path, [FakeResponse(body=body)], slept)
    assert client.player_hero_performance(synthetic_account_id) == {
        "matchCount": 120,
        "isAnonymous": False,
        "allHistoryHeroesPerformance": [{"heroId": 14, "matchCount": 120, "winCount": 65}],
        "rankedAllPickHeroesPerformance": [{"heroId": 14, "matchCount": 40, "winCount": 23}],
    }

    sent = client.session.posts[0]["json"]
    assert sent["variables"] == {"id": synthetic_account_id}
    assert "player(steamAccountId: $id)" in sent["query"]
    assert "matchCount" in sent["query"]
    assert "steamAccount" in sent["query"]
    assert "isAnonymous" in sent["query"]
    assert (
        "allHistoryHeroesPerformance: heroesPerformance(take: 200, request: { take: 10000 })"
        in sent["query"]
    )
    assert "rankedAllPickHeroesPerformance: heroesPerformance(" in sent["query"]
    assert "take: 200," in sent["query"]
    assert "request: { take: 10000, gameModeIds: [ALL_PICK_RANKED] }" in sent["query"]


@pytest.mark.parametrize(
    "player",
    [
        [],
        {
            "matchCount": 10,
            "steamAccount": None,
            "allHistoryHeroesPerformance": [],
            "rankedAllPickHeroesPerformance": [],
        },
        {
            "matchCount": 10,
            "steamAccount": {"isAnonymous": "no"},
            "allHistoryHeroesPerformance": [],
            "rankedAllPickHeroesPerformance": [],
        },
        {
            "matchCount": 10,
            "steamAccount": {"isAnonymous": False},
            "allHistoryHeroesPerformance": [{"heroId": 1, "matchCount": 2, "winCount": 3}],
            "rankedAllPickHeroesPerformance": [],
        },
        {
            "matchCount": 10,
            "steamAccount": {"isAnonymous": False},
            "allHistoryHeroesPerformance": [],
            "rankedAllPickHeroesPerformance": None,
        },
    ],
)
def test_player_hero_performance_rejects_malformed_nested_payloads(tmp_path, slept, player):
    client = make_client(tmp_path, [FakeResponse(body={"data": {"player": player}})], slept)
    with pytest.raises(StratzError, match="Stratz player"):
        client.player_hero_performance(1)
    assert client.cache.entries() == 0


def test_player_hero_performance_rejects_a_missing_player_field(tmp_path, slept):
    client = make_client(tmp_path, [FakeResponse(body={"data": {}})], slept)
    with pytest.raises(StratzError, match="no player field"):
        client.player_hero_performance(1)
    assert client.cache.entries() == 0


def test_malformed_position_payload_is_rejected_before_caching(tmp_path, slept):
    body = {
        "data": {
            "player": {
                "matches": [{"players": [{"heroId": 14, "position": None, "isVictory": True}]}]
            }
        }
    }
    client = make_client(tmp_path, [FakeResponse(body=body)], slept)

    with pytest.raises(StratzError, match="position fields"):
        client.player_positions(1)

    assert client.cache.entries() == 0


def test_malformed_cached_meta_payload_is_a_stable_stratz_error(tmp_path, slept):
    valid = {"data": {"heroStats": {"winWeek": [{"heroId": 14, "matchCount": 10, "winCount": 5}]}}}
    client = make_client(tmp_path, [FakeResponse(body=valid)], slept)
    client.hero_win_rates(8)
    path = next((tmp_path / "cache").glob("*.json"))
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["value"] = {"heroStats": None}
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(StratzError, match="heroStats was not an object"):
        client.hero_win_rates(8)
    assert client.calls_made == 1


def test_malformed_cached_player_payload_is_a_stable_stratz_error(tmp_path, slept):
    valid = {
        "data": {
            "player": {
                "matchCount": 1,
                "steamAccount": {"isAnonymous": False},
                "allHistoryHeroesPerformance": [{"heroId": 14, "matchCount": 1, "winCount": 1}],
                "rankedAllPickHeroesPerformance": [{"heroId": 14, "matchCount": 1, "winCount": 1}],
            }
        }
    }
    client = make_client(tmp_path, [FakeResponse(body=valid)], slept)
    client.player_hero_performance(1)
    path = next((tmp_path / "cache").glob("*.json"))
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["value"] = {"player": []}
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(StratzError, match="player was not an object"):
        client.player_hero_performance(1)
    assert client.calls_made == 1


@pytest.mark.parametrize("cached_value", [[], None, 7, "data"])
def test_a_non_object_cached_value_is_a_stable_stratz_error(tmp_path, slept, cached_value):
    client = make_client(tmp_path, [FakeResponse(body={"data": {"x": 1}})], slept)
    client.query("{ x }")
    path = next((tmp_path / "cache").glob("*.json"))
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["value"] = cached_value
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(StratzError, match="cached data was not an object"):
        client.query("{ x }")
    assert client.calls_made == 1


def test_verify_access_runs_all_production_query_paths(tmp_path, slept):
    meta = {
        "data": {"heroStats": {"winWeek": [{"heroId": 14, "matchCount": 900, "winCount": 500}]}}
    }
    player = {
        "data": {
            "player": {
                "matchCount": 1,
                "steamAccount": {"isAnonymous": False},
                "allHistoryHeroesPerformance": [{"heroId": 14, "matchCount": 1, "winCount": 1}],
                "rankedAllPickHeroesPerformance": [{"heroId": 14, "matchCount": 1, "winCount": 1}],
            }
        }
    }
    positions = {"data": {"player": {"matches": []}}}
    client = make_client(
        tmp_path,
        [FakeResponse(body=meta), FakeResponse(body=player), FakeResponse(body=positions)],
        slept,
    )
    assert client.verify_access(account_id=42) == {
        "ok": True,
        "meta_hero_count": 1,
        "player_found": True,
        "position_rows": 0,
    }
    assert "heroStats" in client.session.posts[0]["json"]["query"]
    assert "allHistoryHeroesPerformance" in client.session.posts[1]["json"]["query"]
    assert "rankedAllPickHeroesPerformance" in client.session.posts[1]["json"]["query"]
    assert client.session.posts[1]["json"]["variables"] == {"id": 42}
    assert "matches(request:" in client.session.posts[2]["json"]["query"]
    assert client.session.posts[2]["json"]["variables"] == {"id": 42, "take": 1}
