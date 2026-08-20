"""Stratz transport tests. Offline: the session is a fake.

These pin the *contract* - auth, retries, GraphQL error handling, caching - not
the schema. Whether `winWeek` still exists upstream is what `verify_access()` is
for; a unit test cannot know, and pretending otherwise would be theatre.
"""

from __future__ import annotations

import json

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


def make_client(tmp_path, script, slept, token="tok"):
    return StratzClient(
        token=token,
        cache_dir=tmp_path / "cache",
        session=FakeSession(script),
        sleep=slept.append,
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


def test_a_200_that_is_not_json_is_an_error(tmp_path, slept):
    client = make_client(tmp_path, [FakeResponse(bad_json=True)], slept)
    with pytest.raises(StratzError, match="not JSON"):
        client.query("{ x }")


def test_results_are_cached(tmp_path, slept):
    client = make_client(tmp_path, [FakeResponse(body={"data": {"x": 1}})], slept)
    client.query("{ x }")
    client.query("{ x }")
    assert client.calls_made == 1


def test_the_token_never_reaches_the_cache(tmp_path, slept):
    secret = "super-secret-token"
    client = make_client(tmp_path, [FakeResponse(body={"data": {"x": 1}})], slept, token=secret)
    client.query("{ x }")
    for entry in (tmp_path / "cache").glob("*.json"):
        assert secret not in entry.read_text(encoding="utf-8")


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


def test_verify_access_reports_whether_the_schema_still_answers(tmp_path, slept):
    body = {"data": {"constants": {"heroes": [{"id": 1, "displayName": "Anti-Mage"}]}}}
    client = make_client(tmp_path, [FakeResponse(body=body)], slept)
    assert client.verify_access() == {"ok": True, "hero_count": 1}
