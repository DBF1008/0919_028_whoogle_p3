import json

import pytest

from app import app
from app.models.endpoint import Endpoint
from app.utils.misc import get_client_ip
from app.utils.rate_limit import RateLimitConfig, RateLimiter, TokenBucket


def configure_limiter(monkeypatch, **env):
    """Rebuild the app limiter from WHOOGLE_RATE_LIMIT_* env values."""
    for key in (
        "WHOOGLE_RATE_LIMIT",
        "WHOOGLE_RATE_LIMIT_IP_BURST",
        "WHOOGLE_RATE_LIMIT_IP_RATE",
        "WHOOGLE_RATE_LIMIT_SESSION_BURST",
        "WHOOGLE_RATE_LIMIT_SESSION_RATE",
        "WHOOGLE_RATE_LIMIT_MAX_BUCKETS",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    limiter = RateLimiter(RateLimitConfig.from_env())
    app.config["RATE_LIMITER"] = limiter
    return limiter


# ---------------------------------------------------------------------------
# Token bucket mechanics
# ---------------------------------------------------------------------------


def test_token_bucket_burst_and_refund():
    bucket = TokenBucket(capacity=3, refill_per_sec=1.0)
    assert all(bucket.consume(now=0.0) for _ in range(3))
    # Exhausted bucket denies the next request...
    assert bucket.consume(now=0.0) is False
    # ...and reports a positive retry window.
    assert bucket.retry_after(now=0.0) >= 1


def test_token_bucket_refills_over_time():
    bucket = TokenBucket(capacity=2, refill_per_sec=2.0, now=0.0)
    assert bucket.consume(now=0.0)
    assert bucket.consume(now=0.0)
    assert bucket.consume(now=0.0) is False
    # Half a second at 2 tokens/sec restores one token.
    assert bucket.consume(now=0.5)
    # Capacity is never exceeded.
    assert bucket.consume(now=100.0)
    assert bucket.consume(now=100.0)
    assert bucket.consume(now=100.0) is False


def test_token_bucket_retry_after_shrinks():
    bucket = TokenBucket(capacity=1, refill_per_sec=1.0, now=0.0)
    assert bucket.consume(now=0.0)
    assert bucket.retry_after(now=0.0) == 1
    # No wait left once a token has regenerated.
    assert bucket.retry_after(now=1.0) == 0


# ---------------------------------------------------------------------------
# RateLimiter dual-layer behavior
# ---------------------------------------------------------------------------


def test_limiter_independent_ip_buckets():
    limiter = RateLimiter(
        RateLimitConfig(
            ip_capacity=2, ip_refill_per_sec=0.0, session_capacity=10, session_refill_per_sec=0.0
        )
    )
    assert limiter.check("1.1.1.1", "s1")[0]
    assert limiter.check("1.1.1.1", "s1")[0]
    allowed, retry_after, layer = limiter.check("1.1.1.1", "s1")
    assert not allowed and layer == "ip" and retry_after >= 1
    # A different IP still has its full burst allowance.
    assert limiter.check("2.2.2.2", "s2")[0]


def test_limiter_session_layer_independent_of_ip():
    limiter = RateLimiter(
        RateLimitConfig(
            ip_capacity=100, ip_refill_per_sec=100.0, session_capacity=2, session_refill_per_sec=0.0
        )
    )
    assert limiter.check("1.1.1.1", "s1")[0]
    assert limiter.check("1.1.1.1", "s2")[0]
    assert limiter.check("1.1.1.1", "s1")[0]
    allowed, _retry_after, layer = limiter.check("1.1.1.1", "s1")
    assert not allowed and layer == "session"
    # Another session behind the same IP is unaffected.
    assert limiter.check("1.1.1.1", "s3")[0]
    assert limiter.limited_session == 1


def test_limiter_disabled_allows_everything():
    limiter = RateLimiter(RateLimitConfig(enabled=False, ip_capacity=1))
    for _ in range(50):
        assert limiter.check("1.1.1.1", "s1")[0]
    assert limiter.stats()["enabled"] is False


def test_limiter_max_buckets_evicts_idle_entries():
    limiter = RateLimiter(
        RateLimitConfig(
            ip_capacity=5,
            ip_refill_per_sec=0.0,
            session_capacity=5,
            session_refill_per_sec=0.0,
            max_buckets=2,
        )
    )
    limiter.check("1.1.1.1", None)
    limiter.check("2.2.2.2", None)
    limiter.check("3.3.3.3", None)
    assert len(limiter._ip_buckets) == 2


# ---------------------------------------------------------------------------
# Environment variable configuration
# ---------------------------------------------------------------------------


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("WHOOGLE_RATE_LIMIT", "0")
    monkeypatch.setenv("WHOOGLE_RATE_LIMIT_IP_BURST", "12")
    monkeypatch.setenv("WHOOGLE_RATE_LIMIT_IP_RATE", "6")
    monkeypatch.setenv("WHOOGLE_RATE_LIMIT_SESSION_BURST", "4")
    monkeypatch.setenv("WHOOGLE_RATE_LIMIT_SESSION_RATE", "3")
    cfg = RateLimitConfig.from_env()
    assert cfg.enabled is False
    assert cfg.ip_capacity == 12
    assert cfg.ip_refill_per_sec == pytest.approx(0.1)
    assert cfg.session_capacity == 4
    assert cfg.session_refill_per_sec == pytest.approx(0.05)


def test_config_from_env_invalid_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("WHOOGLE_RATE_LIMIT_IP_BURST", "not-a-number")
    cfg = RateLimitConfig.from_env()
    assert cfg.ip_capacity == 30


# ---------------------------------------------------------------------------
# Flask route integration
# ---------------------------------------------------------------------------


def test_search_returns_429_with_retry_after(client, monkeypatch):
    configure_limiter(
        monkeypatch,
        WHOOGLE_RATE_LIMIT_IP_BURST=2,
        WHOOGLE_RATE_LIMIT_IP_RATE=1,
        WHOOGLE_RATE_LIMIT_SESSION_BURST=100,
        WHOOGLE_RATE_LIMIT_SESSION_RATE=100,
    )

    assert client.get(f"/{Endpoint.search}?q=a").status_code == 200
    assert client.get(f"/{Endpoint.search}?q=b").status_code == 200
    rv = client.get(f"/{Endpoint.search}?q=c")
    assert rv.status_code == 429
    assert int(rv.headers["Retry-After"]) >= 1


def test_autocomplete_returns_429(client, monkeypatch):
    configure_limiter(
        monkeypatch,
        WHOOGLE_RATE_LIMIT_IP_BURST=1,
        WHOOGLE_RATE_LIMIT_IP_RATE=1,
        WHOOGLE_RATE_LIMIT_SESSION_BURST=100,
        WHOOGLE_RATE_LIMIT_SESSION_RATE=100,
    )

    assert client.get(f"/{Endpoint.autocomplete}?q=who").status_code == 200
    rv = client.get(f"/{Endpoint.autocomplete}?q=who")
    assert rv.status_code == 429
    assert "Retry-After" in rv.headers


def test_other_endpoints_are_not_rate_limited(client, monkeypatch):
    configure_limiter(
        monkeypatch,
        WHOOGLE_RATE_LIMIT_IP_BURST=1,
        WHOOGLE_RATE_LIMIT_IP_RATE=1,
        WHOOGLE_RATE_LIMIT_SESSION_BURST=100,
        WHOOGLE_RATE_LIMIT_SESSION_RATE=100,
    )
    # /healthz is exempt and must remain reachable while searches are blocked.
    for _ in range(10):
        rv = client.get(f"/{Endpoint.healthz}")
        assert rv.status_code == 200


def test_rate_limiting_is_per_ip(client, monkeypatch):
    configure_limiter(
        monkeypatch,
        WHOOGLE_RATE_LIMIT_IP_BURST=1,
        WHOOGLE_RATE_LIMIT_IP_RATE=1,
        WHOOGLE_RATE_LIMIT_SESSION_BURST=100,
        WHOOGLE_RATE_LIMIT_SESSION_RATE=100,
    )

    assert (
        client.get(f"/{Endpoint.search}?q=a", headers={"X-Forwarded-For": "10.0.0.1"}).status_code
        == 200
    )
    assert (
        client.get(f"/{Endpoint.search}?q=b", headers={"X-Forwarded-For": "10.0.0.1"}).status_code
        == 429
    )
    # A different client IP is unaffected.
    assert (
        client.get(f"/{Endpoint.search}?q=c", headers={"X-Forwarded-For": "10.0.0.2"}).status_code
        == 200
    )


def test_429_json_for_json_accept(client, monkeypatch):
    configure_limiter(
        monkeypatch,
        WHOOGLE_RATE_LIMIT_IP_BURST=1,
        WHOOGLE_RATE_LIMIT_IP_RATE=1,
        WHOOGLE_RATE_LIMIT_SESSION_BURST=100,
        WHOOGLE_RATE_LIMIT_SESSION_RATE=100,
    )

    client.get(f"/{Endpoint.search}?q=a")
    rv = client.get(f"/{Endpoint.search}?q=b", headers={"Accept": "application/json"})
    assert rv.status_code == 429
    payload = rv.get_json()
    assert payload["error"] is True
    assert payload["retry_after"] >= 1


def test_healthz_exposes_rate_limit_stats(client, monkeypatch):
    configure_limiter(
        monkeypatch,
        WHOOGLE_RATE_LIMIT_IP_BURST=5,
        WHOOGLE_RATE_LIMIT_IP_RATE=10,
        WHOOGLE_RATE_LIMIT_SESSION_BURST=5,
        WHOOGLE_RATE_LIMIT_SESSION_RATE=10,
    )

    client.get(f"/{Endpoint.search}?q=a")

    rv = client.get(f"/{Endpoint.healthz}")
    payload = json.loads(rv.data)
    assert payload["status"] == "ok"
    stats = payload["rate_limit"]
    assert stats["enabled"] is True
    assert stats["requests_allowed"] >= 1
    assert stats["ip"]["burst"] == 5
    assert stats["ip"]["rate_per_min"] == 10
    assert stats["session"]["burst"] == 5


def test_config_disable_enforces_ip_layer_only(client, monkeypatch):
    limiter = configure_limiter(
        monkeypatch,
        WHOOGLE_RATE_LIMIT_IP_BURST=1,
        WHOOGLE_RATE_LIMIT_IP_RATE=1,
        WHOOGLE_RATE_LIMIT_SESSION_BURST=100,
        WHOOGLE_RATE_LIMIT_SESSION_RATE=100,
    )
    monkeypatch.setitem(app.config, "CONFIG_DISABLE", True)

    assert client.get(f"/{Endpoint.search}?q=a").status_code == 200
    assert client.get(f"/{Endpoint.search}?q=b").status_code == 429
    # No session buckets should have been created in config-locked mode.
    assert len(limiter._session_buckets) == 0


def test_rate_limit_runs_before_auth_decorators(client, monkeypatch):
    # A blocked unauthenticated request must receive 429, never a 401,
    # proving the before_request limiter precedes the decorator chain.
    monkeypatch.setenv("WHOOGLE_USER", "admin")
    monkeypatch.setenv("WHOOGLE_PASS", "secret")
    configure_limiter(
        monkeypatch,
        WHOOGLE_RATE_LIMIT_IP_BURST=1,
        WHOOGLE_RATE_LIMIT_IP_RATE=1,
        WHOOGLE_RATE_LIMIT_SESSION_BURST=100,
        WHOOGLE_RATE_LIMIT_SESSION_RATE=100,
    )

    client.get(f"/{Endpoint.search}?q=a")
    rv = client.get(f"/{Endpoint.search}?q=b")
    assert rv.status_code == 429
    assert "WWW-Authenticate" not in rv.headers


# ---------------------------------------------------------------------------
# Proxy / X-Forwarded-For handling
# ---------------------------------------------------------------------------


def test_get_client_ip_prefers_forwarded_for():
    with app.test_request_context("/", headers={"X-Forwarded-For": "203.0.113.7, 10.0.0.1"}):
        from flask import request

        assert get_client_ip(request) == "203.0.113.7"


def test_get_client_ip_skips_garbage_forwarded_entries():
    with app.test_request_context("/", headers={"X-Forwarded-For": " , , 198.51.100.23, 10.0.0.1"}):
        from flask import request

        assert get_client_ip(request) == "198.51.100.23"


def test_get_client_ip_falls_back_to_remote_addr():
    with app.test_request_context("/", environ_overrides={"REMOTE_ADDR": "127.0.0.1"}):
        from flask import request

        assert get_client_ip(request) == "127.0.0.1"
