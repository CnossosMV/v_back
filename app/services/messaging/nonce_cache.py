"""
Nonce Cache Service for Messaging Middleware

Provides TTL-based caching for tracking used nonces to prevent replay attacks.
Nonces are stored with a configurable TTL (default 5 minutes) matching
the HMAC timestamp validation window.
"""
import time
import threading
from typing import Optional


class NonceCache:
    """
    Simple TTL cache for tracking used nonces.

    Used to prevent replay attacks by ensuring each nonce is only used once
    within the timestamp validation window (5 minutes by default).

    Thread-safe implementation using locks.
    """

    def __init__(self):
        self._cache: dict = {}  # nonce -> expiry_time
        self._lock = threading.Lock()
        self._last_cleanup = time.time()
        self._cleanup_interval = 60  # Cleanup every minute

    def has_been_used(self, nonce: str) -> bool:
        """
        Check if a nonce has already been used (and is still within TTL).

        Args:
            nonce: The nonce string to check (format: "api_key_id:nonce_value")

        Returns:
            True if the nonce has been used and hasn't expired yet
        """
        with self._lock:
            now = time.time()

            # Periodic cleanup
            if now - self._last_cleanup > self._cleanup_interval:
                self._cleanup()
                self._last_cleanup = now

            if nonce in self._cache:
                # Check if it's still valid (not expired)
                if self._cache[nonce] > now:
                    return True
                else:
                    # Expired, remove it
                    del self._cache[nonce]

            return False

    def mark_used(self, nonce: str, ttl_seconds: int = 300):
        """
        Mark a nonce as used with a TTL.

        Args:
            nonce: The nonce string to mark (format: "api_key_id:nonce_value")
            ttl_seconds: Time-to-live in seconds (default 300 = 5 minutes)
        """
        with self._lock:
            self._cache[nonce] = time.time() + ttl_seconds

    def check_and_mark(self, nonce: str, ttl_seconds: int = 300) -> bool:
        """
        Atomically check if a nonce has been used and mark it if not.

        Args:
            nonce: The nonce string to check and mark
            ttl_seconds: Time-to-live in seconds

        Returns:
            True if the nonce was already used (reject request)
            False if the nonce is new (allow request, now marked as used)
        """
        with self._lock:
            now = time.time()

            # Periodic cleanup
            if now - self._last_cleanup > self._cleanup_interval:
                self._cleanup()
                self._last_cleanup = now

            # Check if already used
            if nonce in self._cache:
                if self._cache[nonce] > now:
                    return True  # Already used
                # Expired, will be overwritten

            # Mark as used
            self._cache[nonce] = now + ttl_seconds
            return False  # Not previously used

    def _cleanup(self):
        """
        Remove expired entries from the cache.
        Called periodically to prevent memory growth.
        """
        now = time.time()
        expired_keys = [
            key for key, expiry in self._cache.items()
            if expiry <= now
        ]
        for key in expired_keys:
            del self._cache[key]

    def size(self) -> int:
        """
        Get the current number of cached nonces.

        Returns:
            Number of nonces currently in cache
        """
        with self._lock:
            return len(self._cache)

    def clear(self):
        """
        Clear all cached nonces.
        """
        with self._lock:
            self._cache.clear()


# Singleton instance for use across the application
nonce_cache = NonceCache()
