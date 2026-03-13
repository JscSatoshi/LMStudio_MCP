"""
Unified MCP Server — SearXNG + Playwright
Combines web search and headless browser into a single MCP service.

Key tool:
  search — query SearXNG for links, then fetch each result page with
           Playwright and return full page content to the AI.

Transport: stdio (default) or SSE (set MCP_TRANSPORT=sse)
"""

import asyncio
import os
from contextlib import asynccontextmanager

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from playwright.async_api import async_playwright

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
SEARXNG_URL     = os.environ.get("SEARXNG_URL", "http://localhost:8081").rstrip("/")
SEARXNG_TIMEOUT = float(os.environ.get("SEARXNG_TIMEOUT", "15"))
PAGE_TIMEOUT    = int(os.environ.get("PAGE_TIMEOUT", "10000"))   # ms (10s — fail fast on slow sites to avoid WebSocket timeout)
FETCH_CONCURRENCY = int(os.environ.get("FETCH_CONCURRENCY", "8"))  # parallel pages (increased to complete faster)

# ---------------------------------------------------------------------------
# Browser lifecycle — start once, reuse across all requests
# ---------------------------------------------------------------------------
_pw      = None
_browser = None
_context = None   # persistent context — shares DNS cache & cookies across pages


async def _warmup() -> None:
    global _pw, _browser, _context
    _pw      = await async_playwright().start()
    _browser = await _pw.chromium.launch(
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
    _context = await _browser.new_context(
        java_script_enabled=True,
        ignore_https_errors=True,
    )


async def _get_context():
    global _pw, _browser, _context
    if _browser is None or not _browser.is_connected():
        await _warmup()
    if _context is None:
        _context = await _browser.new_context(
            java_script_enabled=True,
            ignore_https_errors=True,
        )
    return _context


async def _get_browser():
    global _pw, _browser
    if _browser is None or not _browser.is_connected():
        await _warmup()
    return _browser


@asynccontextmanager
async def _lifespan(app):
    await _warmup()
    yield
    if _http_client and not _http_client.is_closed:
        await _http_client.aclose()
    if _context:
        await _context.close()
    if _browser:
        await _browser.close()
    if _pw:
        await _pw.stop()


# ---------------------------------------------------------------------------
# MCP server
# ---------------------------------------------------------------------------
mcp = FastMCP(
    name="web",
    instructions=(
        "You have web tools. Pick the RIGHT tool for each request:\n"
        "- search(query)       → DEFAULT tool for web searches. Returns titles, URLs, and snippets fast (~1s). Use this first.\n"
        "- deep_search(query)  → Use when you need full page content, not just snippets. Slower (reads pages with a browser).\n"
        "- screenshot(url)     → user wants to SEE a page, capture a visual, or get an image of a website\n"
        "- navigate(url)       → user wants the text content of a specific URL (set format='html' for raw HTML source)\n"
        "- extract_links(url)  → user wants all hyperlinks from a page\n"
        "- extract_text(url, selector) → user wants text from a specific part of a page\n\n"
        "IMPORTANT: When the user says 'screenshot', 'capture', 'show me', or 'what does X look like', "
        "ALWAYS use the screenshot tool — do NOT use search."
    ),
    lifespan=_lifespan,
)

# ---------------------------------------------------------------------------
# SearXNG HTTP helpers
# ---------------------------------------------------------------------------
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            base_url=SEARXNG_URL,
            timeout=SEARXNG_TIMEOUT,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _http_client


async def _searxng_query(params: dict) -> dict:
    params.setdefault("format", "json")
    resp = await _get_http_client().get("/search", params=params)
    resp.raise_for_status()
    return resp.json()


def _dedup(results: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for r in results:
        url = r.get("url", "")
        if url and url in seen:
            continue
        seen.add(url)
        out.append(r)
    return out


def _detect_lang(text: str) -> str:
    """
    Detect query language from Unicode script ranges.
    Mixed CJK + Latin → 'all' (multilingual results).
    Returns a SearXNG language code.
    """
    cjk = 0
    latin = 0
    for ch in text:
        cp = ord(ch)
        if (0x4E00 <= cp <= 0x9FFF or 0x3400 <= cp <= 0x4DBF or 0x20000 <= cp <= 0x2A6DF):
            cjk += 1
        elif 0x3040 <= cp <= 0x30FF:   # Japanese Hiragana/Katakana
            cjk += 1
        elif 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF:  # Korean
            cjk += 1
        elif ch.isalpha() and cp < 0x300:  # Basic Latin / extended Latin
            latin += 1

    if cjk == 0:
        return "en"
    if latin == 0:
        # Pure CJK — pick script
        for ch in text:
            cp = ord(ch)
            if 0x3040 <= cp <= 0x30FF:
                return "ja"
            if 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF:
                return "ko"
        return "zh"
    # Mixed — return all to get multilingual results
    return "all"


# ---------------------------------------------------------------------------
# Playwright page helper
# ---------------------------------------------------------------------------
_BLOCK_TYPES = {"image", "media", "font", "stylesheet", "ping", "websocket", "manifest", "other"}


async def _new_text_page():
    """Page from shared context with non-essential resources blocked — faster text extraction."""
    global _context

    async def _block(route):
        if route.request.resource_type in _BLOCK_TYPES:
            await route.abort()
        else:
            await route.continue_()

    for attempt in range(2):
        ctx  = await _get_context()
        try:
            page = await ctx.new_page()
            await page.route("**/*", _block)
            return page
        except Exception:
            # Context or renderer crashed — drop it and retry with a fresh one
            _context = None
            if attempt == 1:
                raise


async def _page_text(url: str, limit: int, wait_until: str = "domcontentloaded") -> str:
    """Return visible body text of a rendered page, truncated to `limit` chars."""
    page = None
    try:
        page = await _new_text_page()
        await page.goto(url, wait_until=wait_until, timeout=PAGE_TIMEOUT)
        text    = await page.inner_text("body")
        lines   = [l.strip() for l in text.splitlines() if l.strip()]
        content = "\n".join(lines)
        if len(content) > limit:
            content = content[:limit] + f"\n\n[... truncated at {limit} chars]"
        return content
    except Exception as exc:
        return f"[fetch error: {exc}]"
    finally:
        if page:
            await page.close()


async def _fetch_page_text(url: str) -> str:
    """Used by search — 5000 char limit per page + 8s timeout."""
    return await _page_text(url, 5000)


# ---------------------------------------------------------------------------
# TOOL: search — fast, SearXNG snippets only (no browser, ~1s)
# ---------------------------------------------------------------------------
@mcp.tool()
async def search(
    query: str,
    categories: str = "general",
    language: str = "auto",
    safe_search: int = 0,
    max_results: int = 10,
) -> str:
    """
    Quick web search: returns titles, URLs, and text snippets from SearXNG.
    Fast (~1s). Use this by default. Use deep_search when you need full page content.

    Args:
        query:       Search query string.
        categories:  SearXNG categories: general, news, science, images, videos, it, etc.
        language:    Language code (e.g. 'en', 'zh') or 'auto'.
        safe_search: 0 = off, 1 = moderate, 2 = strict.
        max_results: Number of results to return (1–20). Default 10.
    """
    max_results = max(1, min(max_results, 20))
    if language == "auto":
        language = _detect_lang(query)
    params: dict = {
        "q":          query,
        "categories": categories,
        "language":   language,
        "safesearch": safe_search,
    }
    try:
        data    = await _searxng_query(params)
        results = _dedup(data.get("results", []))[:max_results]
    except Exception as exc:
        return f"Search failed: {exc}"

    if not results:
        return "No search results found."

    lines = [f"=== Search: '{query}' — {len(results)} results ===\n"]
    for i, r in enumerate(results, 1):
        title   = r.get("title", "").strip()
        url     = r.get("url", "")
        snippet = r.get("content", "").strip()
        lines.append(f"[{i}] {title}")
        lines.append(f"    {url}")
        if snippet:
            lines.append(f"    {snippet[:300]}")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# TOOL: deep_search — SearXNG + Playwright full page reads (slower)
# ---------------------------------------------------------------------------
@mcp.tool()
async def deep_search(
    query: str,
    categories: str = "general",
    language: str = "auto",
    safe_search: int = 0,
    max_results: int = 3,
) -> str:
    """
    Deep web search: finds links via SearXNG, then reads the full rendered content
    of each page with a headless browser. Use when snippets are not enough.

    Args:
        query:       Search query string.
        categories:  SearXNG categories: general, news, science, etc.
        language:    Language code (e.g. 'en', 'zh') or 'auto'.
        safe_search: 0 = off, 1 = moderate, 2 = strict.
        max_results: Pages to fetch and read (1–5). Default 3. Higher = slower.
    """
    max_results = max(1, min(max_results, 5))
    if language == "auto":
        language = _detect_lang(query)

    # Step 1: Quick links from SearXNG
    params: dict = {
        "q":          query,
        "categories": categories,
        "language":   language,
        "safesearch": safe_search,
    }
    try:
        data    = await _searxng_query(params)
        results = _dedup(data.get("results", []))[:max_results]
    except Exception as exc:
        return f"Search failed: {exc}"

    if not results:
        return "No search results found."

    # Format quick links header
    link_lines = [f"=== Deep Search: '{query}' — reading {len(results)} pages ===\n"]
    for i, r in enumerate(results, 1):
        title   = r.get("title", "").strip()
        url     = r.get("url", "")
        snippet = r.get("content", "").strip()
        link_lines.append(f"[{i}] {title}")
        link_lines.append(f"    {url}")
        if snippet:
            link_lines.append(f"    {snippet[:200]}")
        link_lines.append("")

    # Step 2: Deep-read with per-page timeout
    semaphore = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def bounded_fetch(r: dict) -> tuple[str, str, str]:
        url   = r.get("url", "")
        if not url:
            return r.get("title", ""), "", "[skipped: no URL]"
        title = r.get("title", url)
        try:
            async with semaphore:
                text = await asyncio.wait_for(_fetch_page_text(url), timeout=8.0)
        except asyncio.TimeoutError:
            text = "[page timeout after 8s]"
        except Exception as exc:
            text = f"[error: {exc}]"
        return title, url, text

    tasks = [asyncio.create_task(bounded_fetch(r)) for r in results]
    done, pending = await asyncio.wait(tasks, timeout=30)
    for task in pending:
        task.cancel()
    fetched = [task.result() for task in done]

    # Step 3: Combine
    sections = ["\n".join(link_lines)]
    if fetched:
        sections.append(f"\n=== Full Page Content ({len(fetched)} of {len(results)} pages) ===\n")
        for i, (title, url, text) in enumerate(fetched, 1):
            sections.append(f"--- [{i}] {title}\n    {url}\n\n{text}\n")
    else:
        sections.append("\n[No page content retrieved within timeout]")

    return "\n".join(sections)


# ---------------------------------------------------------------------------
# Browser-only tools (single page interactions)
# ---------------------------------------------------------------------------
@mcp.tool()
async def navigate(
    url: str,
    format: str = "text",
    wait_until: str = "domcontentloaded",
) -> str:
    """
    Navigate to a URL and return its content.

    Args:
        url:        The URL to navigate to.
        format:     'text' for visible page text (default), 'html' for raw HTML source.
        wait_until: 'load', 'domcontentloaded', or 'networkidle'.
    """
    if format == "html":
        page = await _new_text_page()
        try:
            await page.goto(url, wait_until=wait_until, timeout=PAGE_TIMEOUT)
            html = await page.content()
            if len(html) > 50000:
                html = html[:50000] + "\n<!-- truncated at 50000 chars -->"
            return html
        except Exception as exc:
            return f"Failed to fetch {url}: {exc}"
        finally:
            await page.close()

    result = await _page_text(url, 20000, wait_until)
    if result.startswith("[fetch error:"):
        return f"Failed to fetch {url}: {result[14:-1]}"
    return result


@mcp.tool()
async def screenshot(url: str, full_page: bool = False) -> Image:
    """
    Take a screenshot of a web page and return it as an image.

    Args:
        url:       The URL to screenshot.
        full_page: Capture full scrollable page (True) or viewport only (False).
    """
    browser = await _get_browser()
    page    = await browser.new_page(viewport={"width": 1280, "height": 720})
    try:
        try:
            await page.goto(url, wait_until="load", timeout=PAGE_TIMEOUT)
        except Exception:
            await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
        await page.wait_for_timeout(1000)
        buf = await page.screenshot(full_page=full_page)
        return Image(data=buf, format="png")
    except Exception as exc:
        raise RuntimeError(f"Failed to screenshot {url}: {exc}") from exc
    finally:
        await page.close()


@mcp.tool()
async def extract_links(url: str, wait_until: str = "domcontentloaded") -> str:
    """
    Extract all hyperlinks from a web page.

    Args:
        url:        The URL to extract links from.
        wait_until: 'load', 'domcontentloaded', or 'networkidle'.
    """
    page = await _new_text_page()
    try:
        await page.goto(url, wait_until=wait_until, timeout=PAGE_TIMEOUT)
        links = await page.eval_on_selector_all(
            "a[href]",
            "els => els.map(e => ({ text: e.innerText.trim(), href: e.href }))"
            ".filter(l => l.href && !l.href.startsWith('javascript:'))",
        )
        if not links:
            return "No links found on this page."
        lines = []
        for link in links[:200]:
            text = link.get("text", "").replace("\n", " ").strip()
            href = link.get("href", "")
            lines.append(f"- [{text}]({href})" if text else f"- {href}")
        return f"Found {len(links)} links (showing {len(lines)}):\n" + "\n".join(lines)
    except Exception as exc:
        return f"Failed to extract links from {url}: {exc}"
    finally:
        await page.close()


@mcp.tool()
async def extract_text(
    url: str,
    selector: str = "body",
    wait_until: str = "domcontentloaded",
) -> str:
    """
    Extract text from a specific CSS selector on a page.

    Args:
        url:        The URL to extract text from.
        selector:   CSS selector (e.g. 'article', 'main', '#content').
        wait_until: 'load', 'domcontentloaded', or 'networkidle'.
    """
    page = await _new_text_page()
    try:
        await page.goto(url, wait_until=wait_until, timeout=PAGE_TIMEOUT)
        element = page.locator(selector).first
        text    = await element.inner_text(timeout=5000)
        lines   = [l.strip() for l in text.splitlines() if l.strip()]
        content = "\n".join(lines)
        if len(content) > 20000:
            content = content[:20000] + "\n\n[... truncated at 20000 chars]"
        return content
    except Exception as exc:
        return f"Failed to extract text from {url} (selector: {selector}): {exc}"
    finally:
        await page.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "sse":
        mcp.settings.host = os.environ.get("MCP_HOST", "0.0.0.0")
        mcp.settings.port = int(os.environ.get("MCP_PORT", "3000"))
    mcp.run(transport=transport)
