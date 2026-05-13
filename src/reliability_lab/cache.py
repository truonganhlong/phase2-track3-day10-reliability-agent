from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Shared utilities — use these in both ResponseCache and SharedRedisCache
# ---------------------------------------------------------------------------

PRIVACY_PATTERNS = re.compile(
    r"\b(balance|password|credit.card|ssn|social.security|user.\d+|account.\d+)\b",
    re.IGNORECASE,
)


def _is_uncacheable(query: str) -> bool:
    """Return True if query contains privacy-sensitive keywords."""
    return bool(PRIVACY_PATTERNS.search(query))


def _looks_like_false_hit(query: str, cached_key: str) -> bool:
    """Return True if query and cached key contain different 4-digit numbers (years, IDs)."""
    nums_q = set(re.findall(r"\b\d{4}\b", query))
    nums_c = set(re.findall(r"\b\d{4}\b", cached_key))
    return bool(nums_q and nums_c and nums_q != nums_c)


def _tokens(text: str) -> set[str]:
    """Normalize text into deterministic word/number tokens."""
    return set(re.findall(r"[a-z0-9]+", text.lower()))


def _char_ngrams(text: str, size: int = 3) -> set[str]:
    """Return compact character n-grams for typo-tolerant similarity."""
    normalized = re.sub(r"\s+", " ", text.lower()).strip()
    if len(normalized) < size:
        return {normalized} if normalized else set()
    return {normalized[i : i + size] for i in range(len(normalized) - size + 1)}


def _is_high_risk_metadata(metadata: dict[str, str]) -> bool:
    """Return True when caller metadata marks an entry unsafe for reuse."""
    risk = metadata.get("expected_risk", "").lower()
    return risk in {"privacy", "high", "sensitive"}


# ---------------------------------------------------------------------------
# In-memory cache (existing)
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class CacheEntry:
    key: str
    value: str
    created_at: float
    metadata: dict[str, str]


class ResponseCache:
    """Simple in-memory cache with TTL, similarity lookup, and safety guardrails."""

    def __init__(self, ttl_seconds: int, similarity_threshold: float):
        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self._entries: list[CacheEntry] = []
        self.false_hit_log: list[dict[str, object]] = []

    def get(self, query: str) -> tuple[str | None, float]:
        if _is_uncacheable(query):
            return None, 0.0
        best_value: str | None = None
        best_key: str | None = None
        best_score = 0.0
        now = time.time()
        self._entries = [e for e in self._entries if now - e.created_at <= self.ttl_seconds]
        for entry in self._entries:
            if query.strip().lower() == entry.key.strip().lower():
                return entry.value, 1.0
            score = self.similarity(query, entry.key)
            if score > best_score:
                best_score = score
                best_key = entry.key
                best_value = entry.value
        if best_score >= self.similarity_threshold:
            if best_key is not None and _looks_like_false_hit(query, best_key):
                self.false_hit_log.append(
                    {"query": query, "cached_key": best_key, "score": round(best_score, 4)}
                )
                return None, best_score
            return best_value, best_score
        return None, best_score

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        metadata = metadata or {}
        if _is_uncacheable(query) or _is_high_risk_metadata(metadata):
            return
        self._entries.append(CacheEntry(query, value, time.time(), metadata))

    @staticmethod
    def similarity(a: str, b: str) -> float:
        """Deterministic similarity using exact, token, and character n-gram overlap."""
        left_normalized = " ".join(a.lower().split())
        right_normalized = " ".join(b.lower().split())
        if left_normalized == right_normalized:
            return 1.0

        left_tokens = _tokens(a)
        right_tokens = _tokens(b)
        left_chars = _char_ngrams(a)
        right_chars = _char_ngrams(b)
        if not left_tokens or not right_tokens:
            return 0.0

        token_score = len(left_tokens & right_tokens) / len(left_tokens | right_tokens)
        char_score = (
            len(left_chars & right_chars) / len(left_chars | right_chars)
            if left_chars and right_chars
            else 0.0
        )
        return (token_score * 0.7) + (char_score * 0.3)


# ---------------------------------------------------------------------------
# Redis shared cache (new)
# ---------------------------------------------------------------------------


class SharedRedisCache:
    """Redis-backed shared cache for multi-instance deployments.

    Data model (suggested):
        Key    = "{prefix}{query_hash}"   (Redis String namespace)
        Value  = Redis Hash with fields:  "query", "response"
        TTL    = Redis EXPIRE (automatic cleanup — no manual eviction)

    For similarity lookup: SCAN all keys with self.prefix, HGET each entry's
    "query" field, compute similarity locally via ResponseCache.similarity().

    Provided helpers:
        _is_uncacheable(query)          — True if privacy-sensitive
        _looks_like_false_hit(q, key)   — True if 4-digit numbers differ
        self._query_hash(query)         — deterministic short hash for Redis key
        ResponseCache.similarity(a, b)  — reuse your improved similarity function
    """

    def __init__(
        self,
        redis_url: str,
        ttl_seconds: int,
        similarity_threshold: float,
        prefix: str = "rl:cache:",
    ):
        import redis as redis_lib

        self.ttl_seconds = ttl_seconds
        self.similarity_threshold = similarity_threshold
        self.prefix = prefix
        self.false_hit_log: list[dict[str, object]] = []
        self._redis: Any = redis_lib.Redis.from_url(redis_url, decode_responses=True)

    def ping(self) -> bool:
        """Check Redis connectivity."""
        try:
            return bool(self._redis.ping())
        except Exception:
            return False

    def get(self, query: str) -> tuple[str | None, float]:
        """Look up a cached response from Redis.

        1. Return (None, 0.0) if _is_uncacheable(query)
        2. Build exact-match key: f"{self.prefix}{self._query_hash(query)}"
        3. Try self._redis.hget(key, "response") — if found return (response, 1.0)
        4. Otherwise self._redis.scan_iter(f"{self.prefix}*") to iterate all cached keys
        5. For each key, HGET "query" field and compute
           ResponseCache.similarity(query, cached_query)
        6. Track best match that is >= self.similarity_threshold
        7. Before returning a match, check _looks_like_false_hit(); if true,
           append to self.false_hit_log and return (None, best_score)
        """
        if _is_uncacheable(query):
            return None, 0.0

        try:
            key = f"{self.prefix}{self._query_hash(query)}"
            exact = self._redis.hget(key, "response")
            if exact is not None:
                return str(exact), 1.0

            best_key: str | None = None
            best_value: str | None = None
            best_score = 0.0
            for redis_key in self._redis.scan_iter(f"{self.prefix}*"):
                cached_query = self._redis.hget(redis_key, "query")
                cached_response = self._redis.hget(redis_key, "response")
                if cached_query is None or cached_response is None:
                    continue
                score = ResponseCache.similarity(query, str(cached_query))
                if score > best_score:
                    best_key = str(cached_query)
                    best_value = str(cached_response)
                    best_score = score

            if best_score >= self.similarity_threshold and best_key is not None:
                if _looks_like_false_hit(query, best_key):
                    self.false_hit_log.append(
                        {"query": query, "cached_key": best_key, "score": round(best_score, 4)}
                    )
                    return None, best_score
                return best_value, best_score
            return None, best_score
        except Exception:
            return None, 0.0

    def set(self, query: str, value: str, metadata: dict[str, str] | None = None) -> None:
        """Store a response in Redis with TTL.

        1. Return immediately if _is_uncacheable(query)
        2. Build key: f"{self.prefix}{self._query_hash(query)}"
        3. self._redis.hset(key, mapping={"query": query, "response": value})
        4. self._redis.expire(key, self.ttl_seconds)
        """
        metadata = metadata or {}
        if _is_uncacheable(query) or _is_high_risk_metadata(metadata):
            return

        try:
            key = f"{self.prefix}{self._query_hash(query)}"
            self._redis.hset(key, mapping={"query": query, "response": value})
            self._redis.expire(key, self.ttl_seconds)
        except Exception:
            return

    def flush(self) -> None:
        """Remove all entries with this cache prefix (for testing)."""
        for key in self._redis.scan_iter(f"{self.prefix}*"):
            self._redis.delete(key)

    def close(self) -> None:
        """Close Redis connection."""
        if self._redis is not None:
            self._redis.close()

    @staticmethod
    def _query_hash(query: str) -> str:
        """Deterministic short hash for a query string."""
        return hashlib.md5(query.lower().strip().encode()).hexdigest()[:12]
