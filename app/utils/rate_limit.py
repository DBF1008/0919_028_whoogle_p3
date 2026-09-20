"""Token bucket rate limiting for search and autocomplete endpoints.

Two independent buckets are checked for every limited request:
  * a per-client-IP bucket (shared by every session behind the address)
  * a per-session bucket (identified by the session uuid)

A token bucket grants short bursts up to its configured capacity while still
enforcing a sustainable average request rate, which provides the burst
tolerance required by interactive search users.
"""

import math
import os
import threading
import time
from dataclasses import dataclass

from flask import current_app


@dataclass(frozen=True)
class RateLimitConfig:
    """Rate limit thresholds, all configurable through environment variables.

    Attributes:
        enabled: Master switch (``WHOOGLE_RATE_LIMIT``).
        ip_capacity: Burst allowance per IP
            (``WHOOGLE_RATE_LIMIT_IP_BURST``).
        ip_refill_per_sec: Sustained per-IP token refill rate
            (``WHOOGLE_RATE_LIMIT_IP_RATE``, requests per minute).
        session_capacity: Burst allowance per session
            (``WHOOGLE_RATE_LIMIT_SESSION_BURST``).
        session_refill_per_sec: Sustained per-session token refill rate
            (``WHOOGLE_RATE_LIMIT_SESSION_RATE``, requests per minute).
        max_buckets: Hard cap on tracked buckets, used as a memory guard
            against key explosion from spoofed IPs/session ids
            (``WHOOGLE_RATE_LIMIT_MAX_BUCKETS``).
    """

    enabled: bool = True
    ip_capacity: int = 30
    ip_refill_per_sec: float = 1.0
    session_capacity: int = 15
    session_refill_per_sec: float = 0.5
    max_buckets: int = 10000

    @staticmethod
    def _read_int(name: str, default: int, minimum: int = 1) -> int:
        try:
            return max(minimum, int(os.getenv(name, str(default))))
        except (TypeError, ValueError):
            return default

    @classmethod
    def from_env(cls) -> "RateLimitConfig":
        enabled = os.getenv("WHOOGLE_RATE_LIMIT", "1").lower() not in ("0", "false", "no", "off")

        ip_capacity = cls._read_int("WHOOGLE_RATE_LIMIT_IP_BURST", 30)
        session_capacity = cls._read_int("WHOOGLE_RATE_LIMIT_SESSION_BURST", 15)
        # Rates are expressed in requests per minute for readability.
        ip_rate = cls._read_int("WHOOGLE_RATE_LIMIT_IP_RATE", 60) / 60.0
        session_rate = cls._read_int("WHOOGLE_RATE_LIMIT_SESSION_RATE", 30) / 60.0
        max_buckets = cls._read_int("WHOOGLE_RATE_LIMIT_MAX_BUCKETS", 10000)

        return cls(
            enabled=enabled,
            ip_capacity=ip_capacity,
            ip_refill_per_sec=ip_rate,
            session_capacity=session_capacity,
            session_refill_per_sec=session_rate,
            max_buckets=max_buckets,
        )


class TokenBucket:
    """A lazily refilled token bucket.

    The bucket starts full (capacity tokens). Tokens regenerate continuously
    at ``refill_per_sec`` up to ``capacity``. A request consumes one token.
    """

    __slots__ = ("capacity", "refill_per_sec", "tokens", "updated")

    def __init__(self, capacity: int, refill_per_sec: float, now: float = None):
        self.capacity = float(capacity)
        self.refill_per_sec = float(refill_per_sec)
        self.tokens = float(capacity)
        self.updated = time.monotonic() if now is None else now

    def consume(self, now: float = None) -> bool:
        """Consume one token, refilling the bucket first.

        Returns:
            bool: True if a token was available and consumed, False otherwise.
        """
        now = time.monotonic() if now is None else now
        elapsed = max(0.0, now - self.updated)
        if elapsed > 0:
            self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_per_sec)
            self.updated = now

        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def retry_after(self, now: float = None) -> int:
        """Whole seconds until at least one token is available."""
        now = time.monotonic() if now is None else now
        if self.tokens >= 1.0:
            return 0
        if self.refill_per_sec <= 0:
            # Bucket never refills; clients should back off for a long while.
            return 3600
        elapsed = max(0.0, now - self.updated)
        wait = (1.0 - self.tokens) / self.refill_per_sec - elapsed
        return 0 if wait <= 0 else int(math.ceil(wait))


class RateLimiter:
    """Dual-layer (IP + session) token bucket rate limiter."""

    def __init__(self, config: RateLimitConfig = None):
        self.config = config or RateLimitConfig.from_env()
        self._ip_buckets = {}
        self._session_buckets = {}
        self._lock = threading.Lock()
        # Counters for the /healthz metrics endpoint.
        self.allowed = 0
        self.limited_ip = 0
        self.limited_session = 0

    def reset(self, config: RateLimitConfig = None) -> None:
        """Clear all buckets and counters. Mainly used by tests."""
        with self._lock:
            self.config = config or RateLimitConfig.from_env()
            self._ip_buckets.clear()
            self._session_buckets.clear()
            self.allowed = 0
            self.limited_ip = 0
            self.limited_session = 0

    def _get_bucket(self, buckets, key, capacity, refill_per_sec, now):
        bucket = buckets.get(key)
        if bucket is None:
            # Memory guard: if the table is already full of unrelated keys
            # (e.g. spoofed X-Forwarded-For values), drop idle buckets
            # instead of growing without bound.
            if len(buckets) >= self.config.max_buckets:
                oldest_key = min(buckets, key=lambda k: buckets[k].updated)
                del buckets[oldest_key]
            bucket = TokenBucket(capacity, refill_per_sec, now)
            buckets[key] = bucket
        return bucket

    def check(self, client_ip, session_id=None):
        """Consume one token from the IP and (optionally) session buckets.

        Args:
            client_ip: The real client IP address.
            session_id: The session uuid, or None when session limiting is
                skipped (cookies disabled or config locked down).

        Returns:
            tuple: (allowed: bool, retry_after: int, layer: str). ``layer``
            is 'ip', 'session' or ''.
        """
        if not self.config.enabled:
            with self._lock:
                self.allowed += 1
            return True, 0, ""

        now = time.monotonic()
        with self._lock:
            ip_bucket = self._get_bucket(
                self._ip_buckets,
                f"ip:{client_ip}",
                self.config.ip_capacity,
                self.config.ip_refill_per_sec,
                now,
            )
            if not ip_bucket.consume(now):
                self.limited_ip += 1
                return False, ip_bucket.retry_after(now), "ip"

            if session_id:
                session_bucket = self._get_bucket(
                    self._session_buckets,
                    f"sess:{session_id}",
                    self.config.session_capacity,
                    self.config.session_refill_per_sec,
                    now,
                )
                if not session_bucket.consume(now):
                    # Refund the IP token: the request never reached upstream,
                    # so a single abusive session should not burn the shared
                    # IP allowance.
                    ip_bucket.tokens = min(ip_bucket.capacity, ip_bucket.tokens + 1.0)
                    self.limited_session += 1
                    return (False, session_bucket.retry_after(now), "session")

            self.allowed += 1
            return True, 0, ""

    def stats(self) -> dict:
        """Snapshot of limiter state for observability."""
        with self._lock:
            return {
                "enabled": self.config.enabled,
                "ip": {
                    "burst": self.config.ip_capacity,
                    "rate_per_min": round(self.config.ip_refill_per_sec * 60, 2),
                    "tracked": len(self._ip_buckets),
                },
                "session": {
                    "burst": self.config.session_capacity,
                    "rate_per_min": round(self.config.session_refill_per_sec * 60, 2),
                    "tracked": len(self._session_buckets),
                },
                "requests_allowed": self.allowed,
                "requests_limited_ip": self.limited_ip,
                "requests_limited_session": self.limited_session,
            }


def get_limiter():
    """Return the process-wide limiter attached to the Flask app."""
    return current_app.config["RATE_LIMITER"]
