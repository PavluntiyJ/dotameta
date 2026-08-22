"""Transport-layer tests. Entirely offline: the session is a fake."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from email.utils import format_datetime

import pytest
import requests

from dotameta.opendota import MAX_ATTEMPTS, OpenDotaClient, OpenDotaError

VALID_HERO = {"id": 1, "localized_name": "Anti-Mage"}


class FakeResponse:
    def __init__(self, status=200, payload=None, text="", headers=None, bad_json=False):
        self.status_code = status
        self._payload = payload if payload is not None else []
        self.text = text
        self.headers = headers or {}
        self._bad_json = bad_json

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    def json(self):
        if self._bad_json:
            raise json.JSONDecodeError("no", "", 0)
        return self._payload


class FakeSession:
    """Replays a scripted list of responses or exceptions."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}), "timeout": timeout})
        item = self.script.pop(0) if self.script else FakeResponse()
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def slept():
    """Collects sleep durations so retries are asserted, never waited on."""
    return []


def make_client(tmp_path, script, slept, **kwargs):
    # min_interval=0 so `slept` records retry backoff only, not throttling.
    kwargs.setdefault("min_interval", 0)
    return OpenDotaClient(
        cache_dir=tmp_path / "cache",
        session=FakeSession(script),
        sleep=slept.append,
        clock=lambda: 0.0,
        **kwargs,
    )


def test_a_timeout_is_retried_then_reported_without_a_traceback(tmp_path, slept):
    client = make_client(tmp_path, [requests.Timeout()] * MAX_ATTEMPTS, slept)
    with pytest.raises(OpenDotaError) as caught:
        client.heroes()
    assert "timed out" in str(caught.value)
    # Retried, but never slept after the final failed attempt.
    assert len(slept) == MAX_ATTEMPTS - 1


def test_a_connection_error_recovers_if_a_later_attempt_succeeds(tmp_path, slept):
    script = [requests.ConnectionError(), FakeResponse(payload=[VALID_HERO])]
    client = make_client(tmp_path, script, slept)
    assert client.heroes() == [VALID_HERO]


def test_dns_and_tls_failures_do_not_escape_as_requests_errors(tmp_path, slept):
    client = make_client(tmp_path, [requests.ConnectionError()] * MAX_ATTEMPTS, slept)
    with pytest.raises(OpenDotaError):
        client.heroes()


def test_rate_limiting_honours_retry_after(tmp_path, slept):
    script = [
        FakeResponse(status=429, headers={"Retry-After": "7"}),
        FakeResponse(payload=[VALID_HERO]),
    ]
    client = make_client(tmp_path, script, slept)
    assert client.heroes() == [VALID_HERO]
    assert 7 in slept


@pytest.mark.parametrize("status", [429, 500, 503])
@pytest.mark.parametrize("delay", [12, 300])
def test_retry_after_http_dates_apply_to_rate_limits_and_server_errors(
    tmp_path, slept, status, delay
):
    now = 1_700_000_000
    header = format_datetime(datetime.fromtimestamp(now + delay, UTC), usegmt=True)
    script = [
        FakeResponse(status=status, headers={"Retry-After": header}),
        FakeResponse(payload=[VALID_HERO]),
    ]
    client = make_client(tmp_path, script, slept, wall_clock=lambda: now)

    assert client.heroes() == [VALID_HERO]
    assert slept == [min(delay, 30)]


@pytest.mark.parametrize(
    ("header", "expected"), [("9", 9), ("0" * 5_000 + "9", 9), ("9" * 5_000, 30)]
)
def test_retry_after_delta_seconds_apply_to_server_errors(tmp_path, slept, header, expected):
    script = [
        FakeResponse(status=503, headers={"Retry-After": header}),
        FakeResponse(payload=[VALID_HERO]),
    ]
    client = make_client(tmp_path, script, slept)

    assert client.heroes() == [VALID_HERO]
    assert slept == [expected]


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize(
    "header", ["soon", "-1", "nan", "inf", "-inf", "Tue, 14 Nov 2023 22:13:19 GMT"]
)
def test_an_invalid_or_past_retry_after_falls_back_to_safe_backoff(tmp_path, slept, status, header):
    script = [
        FakeResponse(status=status, headers={"Retry-After": header}),
        FakeResponse(payload=[VALID_HERO]),
    ]
    client = make_client(tmp_path, script, slept, wall_clock=lambda: 1_700_000_000)
    assert client.heroes() == [VALID_HERO]
    assert slept and all(math.isfinite(delay) and delay >= 0 for delay in slept)


def test_server_errors_are_retried(tmp_path, slept):
    script = [FakeResponse(status=503), FakeResponse(payload=[VALID_HERO])]
    client = make_client(tmp_path, script, slept)
    assert client.heroes() == [VALID_HERO]


def test_exhausted_retries_name_the_last_problem(tmp_path, slept):
    client = make_client(tmp_path, [FakeResponse(status=500)] * MAX_ATTEMPTS, slept)
    with pytest.raises(OpenDotaError, match="HTTP 500"):
        client.heroes()


def test_a_200_that_is_not_json_is_an_opendota_error(tmp_path, slept):
    client = make_client(tmp_path, [FakeResponse(bad_json=True)], slept)
    with pytest.raises(OpenDotaError, match="not JSON"):
        client.heroes()


def test_a_200_with_the_wrong_shape_is_rejected(tmp_path, slept):
    """A dict where a list belongs would blow up three modules away."""
    client = make_client(tmp_path, [FakeResponse(payload={"error": "nope"})], slept)
    with pytest.raises(OpenDotaError, match="expected a list"):
        client.heroes()


def test_a_bad_payload_is_not_cached(tmp_path, slept):
    client = make_client(tmp_path, [FakeResponse(payload={"error": "nope"})], slept)
    with pytest.raises(OpenDotaError):
        client.heroes()
    assert client.cache.entries() == 0


@pytest.mark.parametrize(
    ("call", "payload", "category"),
    [
        (lambda client: client.heroes(), [None], "hero row objects"),
        (lambda client: client.heroes(), [{"id": True}], "hero identifier fields"),
        (
            lambda client: client.heroes(),
            [{"id": 1, "localized_name": "Hero", "roles": "Carry"}],
            "hero role fields",
        ),
        (
            lambda client: client.hero_stats(),
            [{"id": 1, "localized_name": "Hero", "pub_pick": 10, "pub_win": -1}],
            "public count fields",
        ),
        (
            lambda client: client.hero_stats(),
            [
                {
                    "id": 1,
                    "localized_name": "Hero",
                    "pub_pick": 10,
                    "pub_win": 5,
                    "pub_pick_trend": [10],
                    "pub_win_trend": [11],
                }
            ],
            "trend fields",
        ),
        (
            lambda client: client.player(1),
            {"rank_tier": 51, "profile": []},
            "profile fields",
        ),
        (
            lambda client: client.player_win_loss(1),
            {"win": True, "lose": 2},
            "win/loss count fields",
        ),
        (
            lambda client: client.player_heroes(1),
            [{"hero_id": 1, "games": 2, "win": 3}],
            "hero count fields",
        ),
        (
            lambda client: client.player_matches(1),
            [{"hero_id": 1, "lane_role": "off"}],
            "match lane fields",
        ),
        (
            lambda client: client.player_matches(1),
            ["not a row"],
            "match row objects",
        ),
    ],
)
def test_nested_endpoint_payloads_are_rejected_before_caching(
    tmp_path, slept, call, payload, category
):
    client = make_client(tmp_path, [FakeResponse(payload=payload)], slept)

    with pytest.raises(OpenDotaError, match=category):
        call(client)

    assert client.cache.entries() == 0


@pytest.mark.parametrize("name", [None, "", "   "])
def test_heroes_require_a_nonempty_localized_name(tmp_path, slept, name):
    client = make_client(
        tmp_path,
        [FakeResponse(payload=[{"id": 1, "localized_name": name}])],
        slept,
    )

    with pytest.raises(OpenDotaError, match="hero text fields"):
        client.heroes()

    assert client.cache.entries() == 0


@pytest.mark.parametrize("name", [None, "", "   "])
def test_hero_stats_require_a_nonempty_localized_name(tmp_path, slept, name):
    payload = [{"id": 1, "localized_name": name, "pub_pick": 10, "pub_win": 5}]
    client = make_client(tmp_path, [FakeResponse(payload=payload)], slept)

    with pytest.raises(OpenDotaError, match="hero text fields"):
        client.hero_stats()

    assert client.cache.entries() == 0


def test_optional_match_fields_may_be_missing_or_null(tmp_path, slept):
    payload = [{"hero_id": 1, "lane_role": None, "radiant_win": None}]
    client = make_client(tmp_path, [FakeResponse(payload=payload)], slept)
    assert client.player_matches(1) == payload


def test_a_legacy_malformed_cache_hit_is_validated(tmp_path, slept):
    client = make_client(tmp_path, [], slept)
    client.cache.set("/heroes?[]", [{"id": "not-an-integer"}])

    with pytest.raises(OpenDotaError, match="hero identifier fields"):
        client.heroes()

    assert client.calls_made == 0


def test_a_client_error_is_not_retried(tmp_path, slept):
    client = make_client(tmp_path, [FakeResponse(status=404, text="missing")], slept)
    with pytest.raises(OpenDotaError, match="404"):
        client.heroes()
    assert slept == []


def test_a_client_error_does_not_reflect_the_response_body(tmp_path, slept):
    secret = "secret=request-context"
    client = make_client(tmp_path, [FakeResponse(status=403, text=secret)], slept)
    with pytest.raises(OpenDotaError) as caught:
        client.heroes()
    assert "HTTP 403" in str(caught.value)
    assert secret not in str(caught.value)


def test_responses_are_served_from_cache_on_the_second_call(tmp_path, slept):
    client = make_client(tmp_path, [FakeResponse(payload=[VALID_HERO])], slept)
    assert client.heroes() == [VALID_HERO]
    assert client.heroes() == [VALID_HERO]
    assert client.calls_made == 1


def test_the_api_key_never_reaches_the_cache_key_or_an_error(tmp_path, slept):
    secret = "super-secret-key"
    client = make_client(tmp_path, [FakeResponse(status=404, text="nope")], slept, api_key=secret)
    with pytest.raises(OpenDotaError) as caught:
        client.heroes()
    assert secret not in str(caught.value)

    ok = make_client(tmp_path, [FakeResponse(payload=[VALID_HERO])], slept, api_key=secret)
    ok.heroes()
    for entry in (tmp_path / "cache").glob("*.json"):
        assert secret not in entry.read_text(encoding="utf-8")


def test_an_api_key_does_not_disable_throttling_entirely(tmp_path, slept):
    """A key raises the ceiling; it does not license unlimited request rates."""
    client = OpenDotaClient(
        api_key="k", cache_dir=tmp_path / "c", session=FakeSession([]), sleep=slept.append
    )
    assert client.min_interval > 0
