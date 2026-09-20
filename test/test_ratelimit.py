import json
import time

import pytest

from app import app
from app.models.endpoint import Endpoint
from app.utils.misc import get_client_ip
from app.utils.ratelimit import (
    RATE_LIMITED_ENDPOINTS,
    TokenBucket,
    rate_limiter,
)


SEARCH_PATH = f'/{Endpoint.search}?q=test'
AUTOCOMPLETE_PATH = f'/{Endpoint.autocomplete}?q=test'
HEALTHZ_PATH = f'/{Endpoint.healthz}'


@pytest.fixture
def app_request_ctx():
    with app.test_request_context(
            '/', environ_overrides={'REMOTE_ADDR': '127.0.0.1'}):
        from flask import request
        yield request


@pytest.fixture
def app_request_ctx_forwarded():
    with app.test_request_context(
            '/',
            headers={'X-Forwarded-For':
                     '198.51.100.5, 10.0.0.1, 10.0.0.2'}):
        from flask import request
        yield request


@pytest.fixture
def tight_ip_limits():
    app.config['RATELIMIT_IP_LIMIT'] = 3
    app.config['RATELIMIT_IP_BURST'] = 3
    app.config['RATELIMIT_IP_WINDOW'] = 60
    # Keep the session layer permissive so only the IP layer is exercised
    app.config['RATELIMIT_SESSION_LIMIT'] = 1000
    app.config['RATELIMIT_SESSION_BURST'] = 1000
    app.config['RATELIMIT_SESSION_WINDOW'] = 60
    yield
    app.config['RATELIMIT_IP_LIMIT'] = 30
    app.config['RATELIMIT_IP_BURST'] = 30
    app.config['RATELIMIT_SESSION_LIMIT'] = 20
    app.config['RATELIMIT_SESSION_BURST'] = 20


@pytest.fixture
def tight_session_limits():
    app.config['RATELIMIT_SESSION_LIMIT'] = 2
    app.config['RATELIMIT_SESSION_BURST'] = 2
    app.config['RATELIMIT_SESSION_WINDOW'] = 60
    app.config['RATELIMIT_IP_LIMIT'] = 1000
    app.config['RATELIMIT_IP_BURST'] = 1000
    app.config['RATELIMIT_IP_WINDOW'] = 60
    yield
    app.config['RATELIMIT_IP_LIMIT'] = 30
    app.config['RATELIMIT_IP_BURST'] = 30
    app.config['RATELIMIT_SESSION_LIMIT'] = 20
    app.config['RATELIMIT_SESSION_BURST'] = 20


# ---------------------------------------------------------------------------
# TokenBucket unit tests
# ---------------------------------------------------------------------------

def test_token_bucket_burst_tolerance():
    bucket = TokenBucket(3, 3 / 60)
    assert all(bucket.consume()[0] for _ in range(3))
    allowed, retry_after = bucket.consume()
    assert not allowed
    assert isinstance(retry_after, int) and retry_after >= 1


def test_token_bucket_refills_over_window():
    bucket = TokenBucket(1, 1 / 10)
    assert bucket.consume()[0]
    assert not bucket.consume()[0]
    # Simulate one full window (10s) passing
    bucket.updated -= 10
    assert bucket.consume()[0]


def test_token_bucket_retry_after_decreases():
    bucket = TokenBucket(1, 1 / 10)
    bucket.consume()
    _, first_wait = bucket.consume()
    time.sleep(0.2)
    _, second_wait = bucket.consume()
    assert second_wait <= first_wait


# ---------------------------------------------------------------------------
# Endpoint coverage
# ---------------------------------------------------------------------------

def test_only_search_and_autocomplete_are_limited():
    def endpoint_for(path):
        adapter = app.url_map.bind('localhost')
        return adapter.match(path)[0]

    assert endpoint_for('/search') in RATE_LIMITED_ENDPOINTS
    assert endpoint_for('/autocomplete') in RATE_LIMITED_ENDPOINTS
    # Healthz and the home page must never be rate limited
    assert endpoint_for('/healthz') not in RATE_LIMITED_ENDPOINTS
    assert endpoint_for('/') not in RATE_LIMITED_ENDPOINTS
    assert endpoint_for('/config') not in RATE_LIMITED_ENDPOINTS


def test_search_requests_succeed_under_default_limits(client):
    for _ in range(5):
        rv = client.get(SEARCH_PATH)
        assert rv.status_code == 200


def test_non_limited_endpoints_unaffected(client):
    for _ in range(3):
        assert client.get(HEALTHZ_PATH).status_code == 200
        assert client.get('/').status_code == 200


# ---------------------------------------------------------------------------
# IP layer
# ---------------------------------------------------------------------------

def test_ip_layer_returns_429_with_retry_after(client, tight_ip_limits):
    for _ in range(3):
        rv = client.get(SEARCH_PATH)
        assert rv.status_code == 200

    rv = client.get(SEARCH_PATH)
    assert rv.status_code == 429
    retry_after = rv.headers.get('Retry-After')
    assert retry_after is not None
    assert int(retry_after) >= 1
    assert b'Rate limit' in rv.data


def test_ip_layer_covers_autocomplete(client, tight_ip_limits):
    statuses = [client.get(AUTOCOMPLETE_PATH).status_code for _ in range(4)]
    assert statuses[-1] == 429
    assert statuses[:-1] == [200, 200, 200]


def test_ip_layer_429_json_response(client, tight_ip_limits):
    for _ in range(3):
        client.get(SEARCH_PATH)

    rv = client.get(SEARCH_PATH, headers={'Accept': 'application/json'})
    assert rv.status_code == 429
    body = json.loads(rv.data)
    assert body['error'] is True
    assert body['rate_limit_scope'] == 'ip'
    assert body['retry_after'] == int(rv.headers['Retry-After'])


def test_separate_ips_have_independent_buckets(client, tight_ip_limits):
    for _ in range(3):
        rv = client.get(SEARCH_PATH)
        assert rv.status_code == 200
    assert client.get(SEARCH_PATH).status_code == 429

    # A different X-Forwarded-For IP gets a fresh bucket
    rv = client.get(
        SEARCH_PATH,
        headers={'X-Forwarded-For': '203.0.113.7'})
    assert rv.status_code == 200


def test_x_forwarded_for_uses_original_client(client, tight_ip_limits):
    # Proxy chain: "client, proxy1, proxy2" -- leftmost entry wins
    headers = {'X-Forwarded-For': '198.51.100.23, 10.0.0.1, 10.0.0.2'}
    for _ in range(3):
        assert client.get(SEARCH_PATH, headers=headers).status_code == 200
    assert client.get(SEARCH_PATH, headers=headers).status_code == 429

    # Same client from a different proxy chain entry is still limited
    headers_alt = {'X-Forwarded-For': '198.51.100.23, 10.0.0.9'}
    assert client.get(SEARCH_PATH, headers=headers_alt).status_code == 429


def test_get_client_ip_parsing(app_request_ctx):
    request = app_request_ctx
    assert get_client_ip(request) == '127.0.0.1'


def test_get_client_ip_parsing_forwarded(app_request_ctx_forwarded):
    request = app_request_ctx_forwarded
    assert get_client_ip(request) == '198.51.100.5'


# ---------------------------------------------------------------------------
# Session layer
# ---------------------------------------------------------------------------

def test_session_layer_independent_of_ip(client, tight_session_limits):
    # Two different IPs sharing the same session cookie
    for ip in ('192.0.2.1', '192.0.2.2'):
        rv = client.get(
            SEARCH_PATH,
            headers={'X-Forwarded-For': ip,
                     'Accept': 'application/json'})
        assert rv.status_code == 200
    rv = client.get(
        SEARCH_PATH,
        headers={'X-Forwarded-For': '192.0.2.3',
                 'Accept': 'application/json'})
    assert rv.status_code == 429
    body = json.loads(rv.data)
    assert rv.headers.get('Retry-After')
    assert body['rate_limit_scope'] == 'session'


def test_session_limit_scope_reported(client, tight_session_limits):
    for _ in range(2):
        client.get(AUTOCOMPLETE_PATH)
    rv = client.get(
        AUTOCOMPLETE_PATH,
        headers={'Accept': 'application/json'})
    assert rv.status_code == 429
    assert json.loads(rv.data)['rate_limit_scope'] == 'session'


# ---------------------------------------------------------------------------
# Master switch / config disable
# ---------------------------------------------------------------------------

def test_rate_limit_disabled_via_config(client, tight_ip_limits):
    app.config['RATELIMIT_ENABLED'] = False
    for _ in range(10):
        assert client.get(SEARCH_PATH).status_code == 200


def test_rate_limit_enforced_with_config_disabled(client, tight_ip_limits):
    app.config['CONFIG_DISABLE'] = 1
    try:
        for _ in range(3):
            assert client.get(SEARCH_PATH).status_code == 200
        assert client.get(SEARCH_PATH).status_code == 429
    finally:
        app.config['CONFIG_DISABLE'] = 0


def test_rate_limit_runs_before_auth(client, tight_ip_limits, monkeypatch):
    monkeypatch.setenv('WHOOGLE_USER', 'alice')
    monkeypatch.setenv('WHOOGLE_PASS', 'secret')
    # Unauthenticated requests get 401 from auth_required, but the rate
    # limiter in before_request still counts them.
    for _ in range(3):
        assert client.get(SEARCH_PATH).status_code == 401
    # Exhausted bucket: must receive 429, not a 401 auth challenge
    rv = client.get(SEARCH_PATH)
    assert rv.status_code == 429
    assert rv.headers.get('Retry-After')


# ---------------------------------------------------------------------------
# Healthz metrics
# ---------------------------------------------------------------------------

def test_healthz_still_empty_200_for_liveness(client):
    rv = client.get(HEALTHZ_PATH)
    assert rv.status_code == 200
    assert rv.data == b''


def test_healthz_exposes_rate_limit_metrics(client, tight_ip_limits):
    client.get(SEARCH_PATH)
    rv = client.get(f'{HEALTHZ_PATH}?metrics=1')
    assert rv.status_code == 200
    body = json.loads(rv.data)
    assert body['status'] == 'ok'
    metrics = body['rate_limit']
    assert metrics['enabled'] is True
    assert metrics['ip']['limit_per_window'] == 3
    assert metrics['ip']['tracked_clients'] >= 1
    assert metrics['rejected_total'] == 0

    # After exceeding the limit the rejection counter increments
    for _ in range(3):
        client.get(SEARCH_PATH)
    rv = client.get(f'{HEALTHZ_PATH}?metrics=1')
    metrics = json.loads(rv.data)['rate_limit']
    assert metrics['rejected_total'] >= 1


def test_healthz_metrics_json_accept_header(client):
    rv = client.get(HEALTHZ_PATH, headers={'Accept': 'application/json'})
    assert rv.status_code == 200
    assert 'rate_limit' in json.loads(rv.data)
