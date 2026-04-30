"""
Shared web core for both FastAPI and MCP adapters.
"""

from __future__ import annotations

import asyncio
import ipaddress
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright


# ---------------------------------------------------------------------------
# TTL Cache
# ---------------------------------------------------------------------------

class _TTLCache:
    """Simple in-memory TTL cache. Thread-safe for single-threaded async."""

    def __init__(self, ttl_seconds: float = 45.0, max_size: int = 64):
        self._ttl = ttl_seconds
        self._max_size = max_size
        self._store: dict[str, tuple[float, Any]] = {}

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        ts, value = entry
        if time.monotonic() - ts > self._ttl:
            del self._store[key]
            return None
        return value

    def put(self, key: str, value: Any) -> None:
        if len(self._store) >= self._max_size:
            self._evict()
        self._store[key] = (time.monotonic(), value)

    def _evict(self) -> None:
        now = time.monotonic()
        expired = [k for k, (ts, _) in self._store.items() if now - ts > self._ttl]
        for k in expired:
            del self._store[k]
        if len(self._store) >= self._max_size:
            oldest = min(self._store, key=lambda k: self._store[k][0])
            del self._store[oldest]


# ---------------------------------------------------------------------------
# Page Pool
# ---------------------------------------------------------------------------

class _PagePool:
    """Pool of pre-configured Playwright pages for text extraction."""

    _BLOCK_TYPES = {"image", "media", "font", "stylesheet", "ping", "websocket", "manifest", "other"}

    def __init__(self, context_factory, size: int = 4, rotation_callback=None):
        self._context_factory = context_factory
        self._size = size
        self._rotation_callback = rotation_callback
        self._available: asyncio.Queue[Page] = asyncio.Queue(maxsize=size)
        self._initialized = False

    async def warmup(self) -> None:
        while not self._available.empty():
            page = self._available.get_nowait()
            try:
                await page.close()
            except Exception:
                pass
        for _ in range(self._size):
            page = await self._create_page()
            await self._available.put(page)
        self._initialized = True

    async def _create_page(self) -> Page:
        ctx = await self._context_factory()
        page = await ctx.new_page()
        await page.route("**/*", self._block_handler)
        return page

    @staticmethod
    async def _block_handler(route) -> None:
        if route.request.resource_type in _PagePool._BLOCK_TYPES:
            await route.abort()
        else:
            await route.continue_()

    @asynccontextmanager
    async def acquire(self):
        if not self._initialized:
            await self.warmup()
        if self._rotation_callback:
            await self._rotation_callback()
        try:
            page = self._available.get_nowait()
        except asyncio.QueueEmpty:
            page = await self._create_page()

        try:
            yield page
        finally:
            try:
                await page.goto("about:blank", wait_until="commit", timeout=3000)
                try:
                    self._available.put_nowait(page)
                except asyncio.QueueFull:
                    await page.close()
            except Exception:
                try:
                    await page.close()
                except Exception:
                    pass
                try:
                    replacement = await self._create_page()
                    try:
                        self._available.put_nowait(replacement)
                    except asyncio.QueueFull:
                        await replacement.close()
                except Exception:
                    pass

    async def drain(self) -> None:
        """Close all pooled pages (for context rotation)."""
        pages = []
        while not self._available.empty():
            try:
                pages.append(self._available.get_nowait())
            except asyncio.QueueEmpty:
                break
        for p in pages:
            try:
                await p.close()
            except Exception:
                pass
        self._initialized = False


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class CoreConfig:
    searxng_url: str = os.environ.get("SEARXNG_URL", "http://localhost:8081").rstrip("/")
    searxng_timeout: float = float(os.environ.get("SEARXNG_TIMEOUT", "25"))
    page_timeout: int = int(os.environ.get("PAGE_TIMEOUT", "15000"))
    fetch_concurrency: int = int(os.environ.get("FETCH_CONCURRENCY", "5"))
    allow_private_network: bool = os.environ.get("ALLOW_PRIVATE_NETWORK", "false").lower() == "true"
    page_pool_size: int = int(os.environ.get("PAGE_POOL_SIZE", "4"))
    context_rotation_threshold: int = int(os.environ.get("CONTEXT_ROTATION_THRESHOLD", "100"))


# ---------------------------------------------------------------------------
# WebCore
# ---------------------------------------------------------------------------

class WebCore:
    """Transport-agnostic web capabilities shared by HTTP and MCP adapters."""

    _GENERAL_ENGINES = "bing,duckduckgo,brave,yahoo,mojeek,wikipedia,reddit"
    _NEWS_ENGINES = "bing news,duckduckgo news,yahoo news,brave news,wikinews"
    _IT_ENGINES = "bing,duckduckgo,brave,stackoverflow,github,arch linux wiki,hackernews,npm"
    _SCIENCE_ENGINES = "bing,duckduckgo,brave,arxiv,wikipedia"
    _VALID_TIME_RANGES = {"", "day", "week", "month", "year"}
    _ALLOWED_SCHEMES = {"http", "https"}
    _BLOCKED_HOSTS = {"localhost", "127.0.0.1", "::1"}

    def __init__(self, config: Optional[CoreConfig] = None) -> None:
        self.config = config or CoreConfig()
        self._http_client: Optional[httpx.AsyncClient] = None
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._text_context: Optional[BrowserContext] = None
        self._screenshot_context: Optional[BrowserContext] = None
        self._page_pool: _PagePool = _PagePool(
            context_factory=self._get_text_context,
            size=self.config.page_pool_size,
            rotation_callback=self._maybe_rotate_context,
        )
        self._search_cache = _TTLCache(ttl_seconds=45.0, max_size=64)
        self._nav_count = 0
        self._rotating = False

    async def start(self) -> None:
        await self._warmup_browser()
        await self._page_pool.warmup()

    async def stop(self) -> None:
        await self._page_pool.drain()
        if self._http_client and not self._http_client.is_closed:
            await self._http_client.aclose()
        if self._text_context:
            await self._text_context.close()
        if self._screenshot_context:
            await self._screenshot_context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._http_client = None
        self._text_context = None
        self._screenshot_context = None
        self._browser = None
        self._pw = None

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    async def search(
        self,
        query: str,
        categories: str = "general",
        language: str = "auto",
        safe_search: int = 0,
        time_range: str = "",
        max_results: int = 10,
    ) -> dict[str, Any]:
        max_results = max(1, min(max_results, 20))
        if language == "auto":
            language = self._detect_lang(query)
        params: dict[str, Any] = {
            "q": query,
            "categories": categories,
            "language": language,
            "safesearch": safe_search,
        }
        if time_range in self._VALID_TIME_RANGES and time_range:
            params["time_range"] = time_range
        data = await self._searxng_query_with_retry(params, category=categories)
        results = self._dedup(data.get("results", []))
        results.sort(key=lambda r: r.get("score", 0), reverse=True)
        results = results[:max_results]
        unresponsive = data.get("unresponsive_engines", [])
        payload: dict[str, Any] = {
            "query": query,
            "total": data.get("number_of_results", len(results)),
            "results": [
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "content": r.get("content", "")[:500],
                    "published": r.get("publishedDate", "") or r.get("published_date", ""),
                    "engines": r.get("engines", []),
                    "score": r.get("score", 0),
                }
                for r in results
            ],
        }
        if unresponsive and not results:
            payload["unresponsive_engines"] = [[e[0], e[1]] for e in unresponsive]
        return payload

    async def deep_search(
        self,
        query: str,
        categories: str = "general",
        language: str = "auto",
        safe_search: int = 0,
        time_range: str = "",
        max_results: int = 5,
    ) -> dict[str, Any]:
        max_results = max(1, min(max_results, 10))
        if language == "auto":
            language = self._detect_lang(query)

        params: dict[str, Any] = {
            "q": query,
            "categories": categories,
            "language": language,
            "safesearch": safe_search,
        }
        if time_range in self._VALID_TIME_RANGES and time_range:
            params["time_range"] = time_range
        data = await self._searxng_query_with_retry(params, category=categories)

        results = self._dedup(data.get("results", []))
        results.sort(key=lambda r: r.get("score", 0), reverse=True)
        results = results[:max_results]
        if not results:
            return {"query": query, "pages": []}

        semaphore = asyncio.Semaphore(self.config.fetch_concurrency)

        async def fetch_one(r: dict[str, Any]) -> dict[str, str]:
            url = r.get("url", "")
            title = r.get("title", url)
            if err := self.validate_url(url):
                return {"title": title, "url": url, "content": f"[blocked url: {err}]"}
            async with semaphore:
                text = await self._page_text(url, 8000)
            return {"title": title, "url": url, "content": text}

        pages = await asyncio.wait_for(
            asyncio.gather(*[fetch_one(r) for r in results]),
            timeout=45,
        )
        return {"query": query, "pages": list(pages)}

    async def navigate(self, url: str, wait_until: str = "domcontentloaded", format: str = "text") -> dict[str, str]:
        if err := self.validate_url(url):
            return {"url": url, "error": err}

        if format == "html":
            async with self._page_pool.acquire() as page:
                try:
                    await page.goto(url, wait_until=wait_until, timeout=self.config.page_timeout)
                    html = await page.content()
                    if len(html) > 50000:
                        html = html[:50000] + "\n<!-- truncated at 50000 chars -->"
                    return {"url": url, "content": html, "format": "html"}
                except Exception as exc:
                    return {"url": url, "error": str(exc)}

        text = await self._page_text(url, 20000, wait_until)
        if text.startswith("[fetch error:") and text.endswith("]"):
            return {"url": url, "error": text[len("[fetch error: "):-1]}
        return {"url": url, "content": text, "format": "text"}

    async def extract_text(
        self,
        url: str,
        selector: str = "body",
        wait_until: str = "domcontentloaded",
    ) -> dict[str, Any]:
        if err := self.validate_url(url):
            return {"url": url, "error": err}
        async with self._page_pool.acquire() as page:
            try:
                await page.goto(url, wait_until=wait_until, timeout=self.config.page_timeout)
                element = page.locator(selector).first
                text = await element.inner_text(timeout=5000)
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                content = "\n".join(lines)
                if len(content) > 20000:
                    content = content[:20000] + "\n\n[... truncated at 20000 chars]"
                return {"url": url, "selector": selector, "content": content}
            except Exception as exc:
                return {"url": url, "selector": selector, "error": str(exc)}

    async def extract_links(self, url: str, wait_until: str = "domcontentloaded") -> dict[str, Any]:
        if err := self.validate_url(url):
            return {"url": url, "error": err}
        async with self._page_pool.acquire() as page:
            try:
                await page.goto(url, wait_until=wait_until, timeout=self.config.page_timeout)
                links = await page.eval_on_selector_all(
                    "a[href]",
                    "els => els.map(e => ({ text: e.innerText.trim(), href: e.href }))"
                    ".filter(l => l.href && !l.href.startsWith('javascript:'))",
                )
                return {"url": url, "count": len(links), "links": links[:200]}
            except Exception as exc:
                return {"url": url, "error": str(exc)}

    async def headlines(self, url: str, wait_until: str = "domcontentloaded") -> dict[str, Any]:
        if err := self.validate_url(url):
            return {"url": url, "error": err}
        async with self._page_pool.acquire() as page:
            try:
                await page.goto(url, wait_until=wait_until, timeout=self.config.page_timeout)
                items = await page.eval_on_selector_all(
                    "h1, h2, h3, h4, h5, h6",
                    "els => els.map(e => ({ level: parseInt(e.tagName[1]), text: e.innerText.trim() }))"
                    ".filter(h => h.text.length > 0)",
                )
                return {"url": url, "count": len(items), "headlines": items[:200]}
            except Exception as exc:
                return {"url": url, "error": str(exc)}

    async def screenshot(self, url: str, full_page: bool = False) -> bytes:
        if err := self.validate_url(url):
            raise RuntimeError(f"Blocked URL: {err}")
        ctx = await self._get_screenshot_context()
        page = await ctx.new_page()
        try:
            try:
                await page.goto(url, wait_until="load", timeout=self.config.page_timeout)
            except Exception:
                await page.goto(url, wait_until="domcontentloaded", timeout=self.config.page_timeout)
            try:
                await page.wait_for_load_state("networkidle", timeout=2000)
            except Exception:
                await page.wait_for_timeout(300)
            return await page.screenshot(full_page=full_page)
        finally:
            await page.close()

    def validate_url(self, url: str) -> Optional[str]:
        parsed = urlparse(url)
        if parsed.scheme not in self._ALLOWED_SCHEMES:
            return f"URL scheme '{parsed.scheme}' not allowed; use http or https"
        if not parsed.hostname:
            return "URL must include a valid hostname"

        if self.config.allow_private_network:
            return None

        host = parsed.hostname.strip().lower()
        if host in self._BLOCKED_HOSTS:
            return f"Host '{host}' is not allowed"

        try:
            ip = ipaddress.ip_address(host)
            if any(
                (
                    ip.is_private,
                    ip.is_loopback,
                    ip.is_link_local,
                    ip.is_multicast,
                    ip.is_reserved,
                    ip.is_unspecified,
                )
            ):
                return f"IP '{host}' is not allowed"
        except ValueError:
            pass
        return None

    # -----------------------------------------------------------------------
    # Browser lifecycle
    # -----------------------------------------------------------------------

    async def _warmup_browser(self) -> None:
        if self._text_context is not None:
            try:
                await self._text_context.close()
            except Exception:
                pass
            self._text_context = None
        if self._screenshot_context is not None:
            try:
                await self._screenshot_context.close()
            except Exception:
                pass
            self._screenshot_context = None
        if self._browser is not None:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-default-apps",
                "--disable-sync",
                "--no-first-run",
            ],
        )
        self._text_context = await self._browser.new_context(
            java_script_enabled=True,
            ignore_https_errors=True,
        )
        self._screenshot_context = await self._browser.new_context(
            java_script_enabled=True,
            ignore_https_errors=True,
            viewport={"width": 1280, "height": 720},
        )

    async def _get_browser(self) -> Browser:
        if self._browser is None or not self._browser.is_connected():
            await self._warmup_browser()
        if self._browser is None:
            raise RuntimeError("Browser warmup failed")
        return self._browser

    async def _get_text_context(self) -> BrowserContext:
        if self._browser is None or not self._browser.is_connected():
            await self._warmup_browser()
            await self._page_pool.warmup()
        if self._text_context is None:
            browser = await self._get_browser()
            self._text_context = await browser.new_context(
                java_script_enabled=True,
                ignore_https_errors=True,
            )
        return self._text_context

    async def _get_screenshot_context(self) -> BrowserContext:
        if self._browser is None or not self._browser.is_connected():
            await self._warmup_browser()
            await self._page_pool.warmup()
        if self._screenshot_context is None:
            browser = await self._get_browser()
            self._screenshot_context = await browser.new_context(
                java_script_enabled=True,
                ignore_https_errors=True,
                viewport={"width": 1280, "height": 720},
            )
        return self._screenshot_context

    # -----------------------------------------------------------------------
    # Context rotation
    # -----------------------------------------------------------------------

    async def _maybe_rotate_context(self) -> None:
        self._nav_count += 1
        if self._nav_count < self.config.context_rotation_threshold:
            return
        if self._rotating:
            return

        self._rotating = True
        try:
            await self._page_pool.drain()
            old_ctx = self._text_context
            browser = await self._get_browser()
            self._text_context = await browser.new_context(
                java_script_enabled=True,
                ignore_https_errors=True,
            )
            self._nav_count = 0
            await self._page_pool.warmup()
            if old_ctx:
                try:
                    await old_ctx.close()
                except Exception:
                    pass
        finally:
            self._rotating = False

    # -----------------------------------------------------------------------
    # HTTP client
    # -----------------------------------------------------------------------

    def _get_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(
                base_url=self.config.searxng_url,
                timeout=self.config.searxng_timeout,
                http2=True,
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return self._http_client

    # -----------------------------------------------------------------------
    # SearXNG queries (with caching)
    # -----------------------------------------------------------------------

    async def _searxng_query(self, params: dict[str, Any]) -> dict[str, Any]:
        params.setdefault("format", "json")
        cache_key = str(sorted(params.items()))
        cached = self._search_cache.get(cache_key)
        if cached is not None:
            return cached

        for attempt in range(2):
            try:
                resp = await self._get_http_client().get("/search", params=params)
                resp.raise_for_status()
                result = resp.json()
                self._search_cache.put(cache_key, result)
                return result
            except httpx.ReadError:
                if attempt == 1:
                    raise
                if self._http_client and not self._http_client.is_closed:
                    await self._http_client.aclose()
                self._http_client = None
        raise RuntimeError("_searxng_query: unreachable")

    def _engines_for_category(self, category: str) -> str:
        return {
            "news": self._NEWS_ENGINES,
            "it": self._IT_ENGINES,
            "science": self._SCIENCE_ENGINES,
        }.get(category, self._GENERAL_ENGINES)

    async def _searxng_query_with_retry(self, params: dict[str, Any], category: str = "general") -> dict[str, Any]:
        data = await self._searxng_query(params)
        results = data.get("results", [])
        if results:
            return data

        engines = self._engines_for_category(category)
        retry_params = {**params, "engines": engines}
        retry_params.pop("categories", None)
        data = await self._searxng_query(retry_params)
        if data.get("results"):
            return data

        if retry_params.get("language", "all") != "all":
            retry_params["language"] = "all"
            data = await self._searxng_query(retry_params)

        return data

    # -----------------------------------------------------------------------
    # Page text extraction (uses pool)
    # -----------------------------------------------------------------------

    async def _page_text(self, url: str, limit: int, wait_until: str = "domcontentloaded") -> str:
        try:
            async with self._page_pool.acquire() as page:
                await page.goto(url, wait_until=wait_until, timeout=self.config.page_timeout)
                text = await page.inner_text("body")
                lines = [line.strip() for line in text.splitlines() if line.strip()]
                content = "\n".join(lines)
                if len(content) > limit:
                    content = content[:limit] + f"\n\n[... truncated at {limit} chars]"
                return content
        except Exception as exc:
            return f"[fetch error: {exc}]"

    # -----------------------------------------------------------------------
    # Utilities
    # -----------------------------------------------------------------------

    @staticmethod
    def _normalize_url(url: str) -> str:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        path = parsed.path.rstrip("/")
        return f"{host}{path}"

    def _dedup(self, results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        for item in results:
            url = item.get("url", "")
            if not url:
                continue
            key = self._normalize_url(url)
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
        return out

    def _detect_lang(self, text: str) -> str:
        cjk = 0
        latin = 0
        for ch in text:
            cp = ord(ch)
            if (0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF) or (0x20000 <= cp <= 0x2A6DF):
                cjk += 1
            elif 0x3040 <= cp <= 0x30FF:
                cjk += 1
            elif (0xAC00 <= cp <= 0xD7AF) or (0x1100 <= cp <= 0x11FF):
                cjk += 1
            elif ch.isalpha() and cp < 0x300:
                latin += 1

        if cjk == 0:
            return "en"
        if latin == 0:
            for ch in text:
                cp = ord(ch)
                if 0x3040 <= cp <= 0x30FF:
                    return "ja"
                if (0xAC00 <= cp <= 0xD7AF) or (0x1100 <= cp <= 0x11FF):
                    return "ko"
            return "zh"
        return "all"
