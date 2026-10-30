"""Cache-first HTTP with per-host rate limiting, robots.txt respect and budget wiring.

This is the single choke point for every outbound request in the pipeline. Nothing
calls `requests` directly. That guarantees:
  * every response is cached to disk (content-addressed), so a re-run is nearly free;
  * every call is metered against the budget ledger before dispatch;
  * politeness limits and robots.txt are never accidentally bypassed.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import time
import urllib.robotparser as robotparser
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import config
from .budget import BudgetExceeded, BudgetLedger


# Statuses that reflect a temporary condition, never a stable result.
TRANSIENT_STATUSES = frozenset({0, 408, 425, 429, 500, 502, 503, 504})

# Quota/auth failures say something about the KEY, not about the resource. Caching
# one would make a rotated-to key inherit the exhausted key's failure for days.
from .keys import QUOTA_STATUSES  # noqa: E402
NEVER_CACHE = TRANSIENT_STATUSES | QUOTA_STATUSES

# Seconds; doubled per retry attempt.
BACKOFF_BASE = 2.0

# A live-observed failure mode: some hosts (public SearXNG instances especially)
# accept the connection and then trickle bytes slowly enough that urllib3's
# per-read socket timeout never fires, even though `timeout=` was passed --
# `sample` on a stuck pipeline process showed it blocked in SSL poll() for 5+
# minutes against a 10s timeout, with the connection still ESTABLISHED. A plain
# `requests` timeout cannot be trusted as a wall-clock deadline, so the request
# runs in a worker thread and is abandoned (thread left to die on its own) once
# this deadline passes, regardless of what the socket is doing.
_REQUEST_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="http-deadline")


def _bounded_get(url: str, *, params: dict | None = None, timeout: int = 20):
    """Plain `requests.get`, but with the same hard wall-clock deadline as
    HttpClient.request -- for the handful of call sites (apify.py budget/run-limit
    metadata reads) that talk to `requests` directly instead of going through the
    cache-first HttpClient and would otherwise be exposed to the same hang."""
    future = _REQUEST_EXECUTOR.submit(requests.get, url, params=params or {},
                                      timeout=timeout)
    try:
        return future.result(timeout=timeout + 5)
    except FutureTimeoutError:
        future.cancel()
        raise requests.Timeout(f"wall-clock deadline ({timeout + 5}s) exceeded")


@dataclass
class Response:
    status: int
    text: str
    url: str
    from_cache: bool = False
    headers: dict | None = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self):
        try:
            return json.loads(self.text)
        except (ValueError, TypeError):
            return None


class HttpClient:
    def __init__(self, ledger: BudgetLedger, cache_dir: str | Path | None = None):
        self.ledger = ledger
        cfg = config.budget()
        self.cache_dir = Path(cache_dir or cfg["cache"]["dir"])
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.negative_ttl = int(cfg["cache"].get("negative_ttl_days", 2))
        self._last_call: dict[str, float] = {}
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._session = requests.Session()
        self._session.headers["User-Agent"] = config.user_agent()

    # -- cache -------------------------------------------------------------
    def _cache_key(self, method: str, url: str, body: str = "") -> str:
        raw = f"{method.upper()}|{url}|{body}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _cache_path(self, key: str) -> Path:
        # Shard by first two chars to keep directory sizes sane.
        d = self.cache_dir / key[:2]
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{key}.json.gz"

    def _read_cache(self, key: str, ttl_days: int) -> Response | None:
        path = self._cache_path(key)
        if not path.exists():
            return None
        try:
            payload = json.loads(gzip.decompress(path.read_bytes()).decode("utf-8"))
        except (OSError, ValueError):
            return None
        stored = datetime.fromisoformat(payload["stored_at"])
        # Failed responses expire faster -- a 404 today may be a 200 next week.
        effective_ttl = ttl_days if 200 <= payload["status"] < 300 else self.negative_ttl
        if datetime.now(timezone.utc) - stored > timedelta(days=effective_ttl):
            return None
        return Response(status=payload["status"], text=payload["text"],
                        url=payload["url"], from_cache=True,
                        headers=payload.get("headers"))

    def _write_cache(self, key: str, resp: Response) -> None:
        payload = {"stored_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "status": resp.status, "text": resp.text, "url": resp.url,
                   "headers": resp.headers or {}}
        blob = gzip.compress(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
        self._cache_path(key).write_bytes(blob)

    # -- politeness --------------------------------------------------------
    def _throttle(self, provider: str, host: str) -> None:
        rps = float(self.ledger.spec(provider).get("rate_limit_rps") or 0)
        if rps <= 0:
            return
        min_gap = 1.0 / rps
        bucket = f"{provider}:{host}"
        elapsed = time.monotonic() - self._last_call.get(bucket, 0.0)
        if elapsed < min_gap:
            time.sleep(min_gap - elapsed)
        self._last_call[bucket] = time.monotonic()

    def robots_allows(self, url: str) -> bool:
        """Check robots.txt for a site fetch. Fail-open only on fetch errors."""
        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        if origin not in self._robots:
            rp = robotparser.RobotFileParser()
            try:
                r = self._session.get(f"{origin}/robots.txt", timeout=10)
                if r.status_code == 200:
                    rp.parse(r.text.splitlines())
                else:
                    rp = None          # no robots.txt published -> unrestricted
            except requests.RequestException:
                rp = None
            self._robots[origin] = rp
        rp = self._robots[origin]
        return True if rp is None else rp.can_fetch(config.user_agent(), url)

    # -- main entry point --------------------------------------------------
    def get(self, url: str, *, provider: str, params: dict | None = None,
            headers: dict | None = None, ttl_days: int | None = None,
            respect_robots: bool = False, timeout: int = 20,
            context: str = "", cache_exclude: tuple[str, ...] = (),
            retries: int = 2) -> Response:
        return self.request("GET", url, provider=provider, params=params,
                            headers=headers, ttl_days=ttl_days,
                            respect_robots=respect_robots, timeout=timeout,
                            context=context, cache_exclude=cache_exclude,
                            retries=retries)

    def request(self, method: str, url: str, *, provider: str,
                params: dict | None = None, headers: dict | None = None,
                json_body: dict | None = None, ttl_days: int | None = None,
                respect_robots: bool = False, timeout: int = 20,
                context: str = "", retries: int = 2,
                cache_exclude: tuple[str, ...] = ()) -> Response:
        prepared = requests.Request(method, url, params=params or {}).prepare()
        full_url = prepared.url or url
        body = json.dumps(json_body, sort_keys=True) if json_body else ""

        # Credentials must NEVER contribute to the cache key. Tavily carries its
        # api_key in the body and Apify its token in the query string, so rotating
        # a key would otherwise invalidate every cached response for that provider.
        cache_params = {k: v for k, v in (params or {}).items()
                        if k not in cache_exclude}
        cache_url = requests.Request(method, url, params=cache_params).prepare().url or url
        cache_body = json.dumps({k: v for k, v in (json_body or {}).items()
                                 if k not in cache_exclude}, sort_keys=True) \
            if json_body else ""
        key = self._cache_key(method, cache_url, cache_body)

        ttl = ttl_days if ttl_days is not None else int(
            self.ledger.spec(provider).get("cache_ttl_days", 7))

        # 1. Cache first, always. A cache hit costs nothing and is never budgeted.
        cached = self._read_cache(key, ttl)
        if cached is not None:
            self.ledger.record(provider, full_url, cache_hit=True,
                               status=cached.status, context=context)
            return cached

        # 2. Budget gate before any network egress.
        try:
            self.ledger.check(provider)
        except BudgetExceeded:
            raise

        # 3. robots.txt for site crawling.
        if respect_robots and not self.robots_allows(full_url):
            self.ledger.record(provider, full_url, status=999, context="robots_disallow")
            return Response(status=999, text="", url=full_url)

        # Free APIs (MusicBrainz especially) return 503/429 under load. Retrying with
        # backoff turns an intermittent outage into a slow success instead of a
        # spurious REVIEW row.
        resp = Response(status=0, text="", url=full_url)
        for attempt in range(retries + 1):
            self._throttle(provider, urlparse(full_url).netloc)
            try:
                future = _REQUEST_EXECUTOR.submit(
                    self._session.request, method, url, params=params or {},
                    headers=headers or {}, json=json_body, timeout=timeout)
                try:
                    r = future.result(timeout=timeout + 5)
                except FutureTimeoutError:
                    future.cancel()  # no-op if already running; thread is abandoned
                    raise requests.Timeout(
                        f"wall-clock deadline ({timeout + 5}s) exceeded")
                resp = Response(status=r.status_code, text=r.text, url=r.url,
                                headers=dict(r.headers))
            except requests.RequestException as exc:
                resp = Response(status=0, text="", url=full_url)
                self._last_error = str(exc)

            if resp.status not in TRANSIENT_STATUSES or attempt == retries:
                break
            # Honour Retry-After when the server sends one.
            delay = BACKOFF_BASE * (2 ** attempt)
            retry_after = (resp.headers or {}).get("Retry-After") if resp.headers else None
            if retry_after and str(retry_after).isdigit():
                delay = max(delay, min(int(retry_after), 30))
            time.sleep(delay)

        self.ledger.record(provider, full_url, status=resp.status, context=context)
        # Transient failures are NOT facts about the world -- caching a 429 or a 503
        # would poison the cache for days and silently starve a source.
        if resp.status not in NEVER_CACHE:
            self._write_cache(key, resp)
        return resp

    def invalidate(self, method: str, url: str, *, params: dict | None = None,
                   json_body: dict | None = None,
                   cache_exclude: tuple[str, ...] = ()) -> bool:
        """Drop a cached entry so the next identical call really hits the network.

        Needed where a response is account-specific but the credential is excluded
        from the cache key: an empty Apify dataset caused by one account's quota
        would otherwise be replayed to every other account, silently defeating
        key rotation.
        """
        cache_params = {k: v for k, v in (params or {}).items()
                        if k not in cache_exclude}
        cache_url = requests.Request(method, url,
                                     params=cache_params).prepare().url or url
        cache_body = json.dumps({k: v for k, v in (json_body or {}).items()
                                 if k not in cache_exclude}, sort_keys=True) \
            if json_body else ""
        path = self._cache_path(self._cache_key(method, cache_url, cache_body))
        if path.exists():
            path.unlink()
            return True
        return False

    def cache_stats(self) -> dict:
        files = list(self.cache_dir.rglob("*.json.gz"))
        return {"entries": len(files),
                "bytes": sum(f.stat().st_size for f in files)}
