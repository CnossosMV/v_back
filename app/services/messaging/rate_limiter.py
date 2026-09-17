"""
Rate Limiter Service for Messaging Middleware

Implements sliding window rate limiting with in-memory storage.
Enforces rate_limit_per_minute and rate_limit_per_day constraints.
"""
import time
import threading
from typing import Tuple
from collections import defaultdict


class InMemoryRateLimiter:
    """
    Simple in-memory rate limiter with sliding window algorithm.

    Uses two time windows:
    - Per-minute: 60-second sliding window
    - Per-day: 86400-second (24-hour) sliding window

    Thread-safe implementation using locks.
    """

    def __init__(self):
        self._minute_buckets: dict = defaultdict(list)  # key -> list of timestamps
        self._day_buckets: dict = defaultdict(list)
        self._lock = threading.Lock()
        self._last_cleanup = time.time()
        self._cleanup_interval = 300  # Cleanup every 5 minutes

    def check_rate_limit(
        self,
        key: str,
        limit_per_minute: int,
        limit_per_day: int
    ) -> Tuple[bool, str]:
        """
        Check if a request is within rate limits and record it if allowed.

        Args:
            key: Unique identifier for rate limiting (e.g., "api_key:123" or "domain:456")
            limit_per_minute: Maximum requests allowed per minute
            limit_per_day: Maximum requests allowed per day

        Returns:
            Tuple of (allowed: bool, error_message: str)
            - If allowed: (True, "")
            - If rate limited: (False, "Rate limit exceeded: X/minute" or "X/day")
        """
        now = time.time()
        minute_ago = now - 60
        day_ago = now - 86400

        with self._lock:
            # Periodic cleanup of old entries
            if now - self._last_cleanup > self._cleanup_interval:
                self._cleanup_old_entries()
                self._last_cleanup = now

            # Clean old entries for this key
            self._minute_buckets[key] = [
                t for t in self._minute_buckets[key] if t > minute_ago
            ]
            self._day_buckets[key] = [
                t for t in self._day_buckets[key] if t > day_ago
            ]

            # Check minute limit
            minute_count = len(self._minute_buckets[key])
            if minute_count >= limit_per_minute:
                return False, f"Rate limit exceeded: {limit_per_minute}/minute"

            # Check day limit
            day_count = len(self._day_buckets[key])
            if day_count >= limit_per_day:
                return False, f"Rate limit exceeded: {limit_per_day}/day"

            # Record this request
            self._minute_buckets[key].append(now)
            self._day_buckets[key].append(now)

            return True, ""

    def get_usage(self, key: str) -> Tuple[int, int]:
        """
        Get current usage counts for a key.

        Args:
            key: The rate limit key

        Returns:
            Tuple of (minute_count, day_count)
        """
        now = time.time()
        minute_ago = now - 60
        day_ago = now - 86400

        with self._lock:
            minute_count = len([
                t for t in self._minute_buckets[key] if t > minute_ago
            ])
            day_count = len([
                t for t in self._day_buckets[key] if t > day_ago
            ])
            return minute_count, day_count

    def _cleanup_old_entries(self):
        """
        Remove expired entries from all buckets.
        Called periodically to prevent memory growth.
        """
        now = time.time()
        minute_ago = now - 60
        day_ago = now - 86400

        # Clean minute buckets
        keys_to_remove = []
        for key, timestamps in self._minute_buckets.items():
            self._minute_buckets[key] = [t for t in timestamps if t > minute_ago]
            if not self._minute_buckets[key]:
                keys_to_remove.append(key)
        for key in keys_to_remove:
            del self._minute_buckets[key]

        # Clean day buckets
        keys_to_remove = []
        for key, timestamps in self._day_buckets.items():
            self._day_buckets[key] = [t for t in timestamps if t > day_ago]
            if not self._day_buckets[key]:
                keys_to_remove.append(key)
        for key in keys_to_remove:
            del self._day_buckets[key]

    def reset(self, key: str = None):
        """
        Reset rate limit counters.

        Args:
            key: If provided, reset only this key. Otherwise, reset all.
        """
        with self._lock:
            if key:
                self._minute_buckets.pop(key, None)
                self._day_buckets.pop(key, None)
            else:
                self._minute_buckets.clear()
                self._day_buckets.clear()


# Singleton instance for use across the application
rate_limiter = InMemoryRateLimiter()
