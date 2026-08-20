"""Transport-layer tests. Entirely offline: the session is a fake."""

from __future__ import annotations

import json

import pytest
import requests

from dotameta.opendota import MAX_ATTEMPTS, OpenDotaClient, OpenDotaError


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
    script = [requests.ConnectionError(), FakeResponse(payload=[{"id": 1}])]
    client = make_client(tmp_path, script, slept)
    assert client.heroes() == [{"id": 1}]


def test_dns_and_tls_failures_do_not_escape_as_requests_errors(tmp_path, slept):
    client = make_client(tmp_path, [requests.ConnectionError()] * MAX_ATTEMPTS, slept)
    with pytest.raises(OpenDotaError):
        client.heroes()


def test_rate_limiting_honours_retry_after(tmp_path, slept):
    script = [
        FakeResponse(status=429, headers={"Retry-After": "7"}),
        FakeResponse(payload=[{"id": 1}]),
    ]
    client = make_client(tmp_path, script, slept)
    assert client.heroes() == [{"id": 1}]
    assert 7 in slept


def test_a_malformed_retry_after_falls_back_to_backoff(tmp_path, slept):
    script = [
        FakeResponse(status=429, headers={"Retry-After": "soon"}),
        FakeResponse(payload=[{"id": 1}]),
    ]
    client = make_client(tmp_path, script, slept)
    assert client.heroes() == [{"id": 1}]
    assert slept and all(delay > 0 for delay in slept)


def test_server_errors_are_retried(tmp_path, slept):
    script = [FakeResponse(status=503), FakeResponse(payload=[{"id": 1}])]
    client = make_client(tmp_path, script, slept)
    assert client.heroes() == [{"id": 1}]


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


def test_a_client_error_is_not_retried(tmp_path, slept):
    client = make_client(tmp_path, [FakeResponse(status=404, text="missing")], slept)
    with pytest.raises(OpenDotaError, match="404"):
        client.heroes()
    assert slept == []


def test_responses_are_served_from_cache_on_the_second_call(tmp_path, slept):
    client = make_client(tmp_path, [FakeResponse(payload=[{"id": 1}])], slept)
    assert client.heroes() == [{"id": 1}]
    assert client.heroes() == [{"id": 1}]
    assert client.calls_made == 1


def test_the_api_key_never_reaches_the_cache_key_or_an_error(tmp_path, slept):
    secret = "super-secret-key"
    client = make_client(tmp_path, [FakeResponse(status=404, text="nope")], slept, api_key=secret)
    with pytest.raises(OpenDotaError) as caught:
        client.heroes()
    assert secret not in str(caught.value)

    ok = make_client(tmp_path, [FakeResponse(payload=[{"id": 1}])], slept, api_key=secret)
    ok.heroes()
    for entry in (tmp_path / "cache").glob("*.json"):
        assert secret not in entry.read_text(encoding="utf-8")


def test_an_api_key_does_not_disable_throttling_entirely(tmp_path, slept):
    """A key raises the ceiling; it does not license unlimited request rates."""
    client = OpenDotaClient(
        api_key="k", cache_dir=tmp_path / "c", session=FakeSession([]), sleep=slept.append
    )
    assert client.min_interval > 0
