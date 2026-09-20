import threading
import time
from dataclasses import dataclass

# Endpoints protected by the rate limiter. These map to Flask request.endpoint
# values (function names defined in app/routes.py).
RATE_LIMITED_ENDPOINTS = ('search', 'autocomplete')

# Minimum age (seconds) of an idle, fully-refilled bucket before it is removed.
_BUCKET_IDLE_TTL = 300


def _get_int_setting(app_config, key, default, minimum=1):
    try:
        return max(minimum, int(app_config.get(key, default)))
    except (TypeError, ValueError):
        return default


@dataclass
class BucketSettings:
    limit: int
    burst: int
    window: int


class TokenBucket:
    """A token bucket with burst tolerance.

    Tokens are continuously refilled at limit/window tokens per second up to
    a maximum capacity of ``burst``, which allows short request bursts while
    still enforcing the sustained ``limit`` over ``window`` seconds.
    """

    __slots__ = ('capacity', 'refill_rate', 'tokens', 'updated')

    def __init__(self, capacity, refill_rate, tokens=None):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = capacity if tokens is None else tokens
        self.updated = time.monotonic()

    def configure(self, capacity, refill_rate):
        self.capacity = capacity
        self.refill_rate = refill_rate
        if self.tokens > capacity:
            self.tokens = capacity

    def consume(self):
        now = time.monotonic()
        elapsed = now - self.updated
        if elapsed > 0:
            self.tokens = min(
                self.capacity, self.tokens + elapsed * self.refill_rate)
            self.updated = now

        if self.tokens >= 1:
            self.tokens -= 1
            return True, 0

        retry_after = (1 - self.tokens) / self.refill_rate
        return False, max(1, int(retry_after + 0.999))

    @property
    def is_full(self):
        return self.tokens >= self.capacity


class RateLimiter:
    """Two-layer (per-IP and per-session) token bucket rate limiter."""

    def __init__(self):
        self._buckets = {}
        self._lock = threading.Lock()
        self._last_cleanup = time.monotonic()
        self._total_rejected = 0

    def reset(self):
        with self._lock:
            self._buckets = {}
            self._last_cleanup = time.monotonic()
            self._total_rejected = 0

    def _settings(self, app_config, scope):
        prefix = 'RATELIMIT_IP_' if scope == 'ip' else 'RATELIMIT_SESSION_'
        default_limit = 30 if scope == 'ip' else 20
        limit = _get_int_setting(app_config, f'{prefix}LIMIT', default_limit)
        window = _get_int_setting(app_config, f'{prefix}WINDOW', 60)
        burst = _get_int_setting(
            app_config, f'{prefix}BURST', limit)
        return BucketSettings(limit=limit, burst=burst, window=window)

    def _get_bucket(self, scope, key, settings):
        bucket_key = (scope, key)
        refill_rate = settings.limit / settings.window
        bucket = self._buckets.get(bucket_key)
        if bucket is None:
            bucket = TokenBucket(settings.burst, refill_rate)
            self._buckets[bucket_key] = bucket
        else:
            bucket.configure(settings.burst, refill_rate)
        return bucket

    def _maybe_cleanup(self, now):
        # Periodically drop idle, fully-refilled buckets to bound memory.
        # A full bucket can be safely removed since a replacement starts full.
        if now - self._last_cleanup < 60:
            return
        for key in list(self._buckets.keys()):
            bucket = self._buckets[key]
            if bucket.is_full and now - bucket.updated > _BUCKET_IDLE_TTL:
                self._buckets.pop(key, None)
        self._last_cleanup = now

    def check(self, app_config, ip_key, session_key):
        """Consume one token from both layers.

        Returns (allowed, scope, retry_after) where scope identifies which
        layer rejected the request ('ip' or 'session').
        """
        now = time.monotonic()
        with self._lock:
            ip_bucket = self._get_bucket(
                'ip', ip_key, self._settings(app_config, 'ip'))
            ip_allowed, ip_retry = ip_bucket.consume()
            if not ip_allowed:
                self._total_rejected += 1
                self._maybe_cleanup(now)
                return False, 'ip', ip_retry

            if session_key:
                session_bucket = self._get_bucket(
                    'session', session_key,
                    self._settings(app_config, 'session'))
                session_allowed, session_retry = session_bucket.consume()
                if not session_allowed:
                    self._total_rejected += 1
                    self._maybe_cleanup(now)
                    return False, 'session', session_retry

            self._maybe_cleanup(now)
            return True, None, 0

    def get_stats(self, app_config):
        with self._lock:
            ip_buckets = session_buckets = 0
            ip_remaining = session_remaining = 0
            ip_limit = self._settings(app_config, 'ip').limit
            session_limit = self._settings(app_config, 'session').limit
            for (scope, _), bucket in self._buckets.items():
                if scope == 'ip':
                    ip_buckets += 1
                    ip_remaining += int(bucket.tokens)
                else:
                    session_buckets += 1
                    session_remaining += int(bucket.tokens)
            return {
                'enabled': bool(app_config.get('RATELIMIT_ENABLED', True)),
                'ip': {
                    'limit_per_window': ip_limit,
                    'tracked_clients': ip_buckets,
                    'approx_tokens_remaining': ip_remaining,
                },
                'session': {
                    'limit_per_window': session_limit,
                    'tracked_clients': session_buckets,
                    'approx_tokens_remaining': session_remaining,
                },
                'rejected_total': self._total_rejected,
            }


rate_limiter = RateLimiter()
