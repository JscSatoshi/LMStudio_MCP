"""MCP adapter for shared web core."""

import os
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.utilities.types import Image
from web_core import WebCore

core = WebCore()


@asynccontextmanager
async def _lifespan(app):
    await core.start()
    yield
    await core.stop()


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
        "TIPS for better results:\n"
        "- For RECENT or CURRENT topics (news, prices, events, releases), ALWAYS set time_range='week' or 'month'\n"
        "- Use categories='news' for current events, 'it' for programming, 'science' for academic\n"
        "- Set language explicitly (e.g. 'zh', 'ja') if the user's query language differs from the desired result language\n\n"
        "IMPORTANT: When the user says 'screenshot', 'capture', 'show me', or 'what does X look like', "
        "ALWAYS use the screenshot tool — do NOT use search."
    ),
    lifespan=_lifespan,
)

# ---------------------------------------------------------------------------
# TOOL: search — fast, SearXNG snippets only (no browser, ~1s)
# ---------------------------------------------------------------------------
@mcp.tool()
async def search(
    query: str,
    categories: str = "general",
    language: str = "auto",
    safe_search: int = 0,
    time_range: str = "",
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
        time_range:  Time filter: '' (any time), 'day', 'week', 'month', 'year'.
        max_results: Number of results to return (1–20). Default 10.
    """
    try:
        data = await core.search(
            query=query,
            categories=categories,
            language=language,
            safe_search=safe_search,
            time_range=time_range,
            max_results=max_results,
        )
        results = data.get("results", [])
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}".rstrip(": ")
        return f"Search failed: {err}"

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
    time_range: str = "",
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
        time_range:  Time filter: '' (any time), 'day', 'week', 'month', 'year'.
        max_results: Pages to fetch and read (1–10). Default 3. Higher = slower.
    """
    try:
        data = await core.deep_search(
            query=query,
            categories=categories,
            language=language,
            safe_search=safe_search,
            time_range=time_range,
            max_results=max_results,
        )
        pages = data.get("pages", [])
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}".rstrip(": ")
        return f"Search failed: {err}"

    if not pages:
        return "No search results found."

    lines = [f"=== Deep Search: '{query}' - reading {len(pages)} pages ===", ""]
    for i, page in enumerate(pages, 1):
        lines.append(f"--- [{i}] {page.get('title', '')}")
        lines.append(f"    {page.get('url', '')}")
        lines.append("")
        lines.append(page.get("content", ""))
        lines.append("")
    return "\n".join(lines)


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
    result = await core.navigate(url=url, format=format, wait_until=wait_until)
    if "error" in result:
        return result["error"]
    return result.get("content", "")


@mcp.tool()
async def screenshot(url: str, full_page: bool = False) -> Image:
    """
    Take a screenshot of a web page and return it as an image.

    Args:
        url:       The URL to screenshot.
        full_page: Capture full scrollable page (True) or viewport only (False).
    """
    try:
        buf = await core.screenshot(url=url, full_page=full_page)
        return Image(data=buf, format="png")
    except Exception as exc:
        raise RuntimeError(f"Failed to screenshot {url}: {exc}") from exc


@mcp.tool()
async def extract_links(url: str, wait_until: str = "domcontentloaded") -> str:
    """
    Extract all hyperlinks from a web page.

    Args:
        url:        The URL to extract links from.
        wait_until: 'load', 'domcontentloaded', or 'networkidle'.
    """
    result = await core.extract_links(url=url, wait_until=wait_until)
    if "error" in result:
        return result["error"]
    links = result.get("links", [])
    if not links:
        return "No links found on this page."
    lines = []
    for link in links[:200]:
        text = link.get("text", "").replace("\n", " ").strip()
        href = link.get("href", "")
        lines.append(f"- [{text}]({href})" if text else f"- {href}")
    return f"Found {len(links)} links (showing {len(lines)}):\n" + "\n".join(lines)


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
    result = await core.extract_text(url=url, selector=selector, wait_until=wait_until)
    if "error" in result:
        return result["error"]
    return result.get("content", "")


@mcp.tool()
async def headlines(url: str, wait_until: str = "domcontentloaded") -> str:
    """Extract all headings (h1-h6) from a web page."""
    result = await core.headlines(url=url, wait_until=wait_until)
    if "error" in result:
        return f"Failed to extract headlines from {url}: {result['error']}"

    items = result.get("headlines", [])
    if not items:
        return "No headlines found on this page."

    lines = [f"Found {result.get('count', len(items))} headlines (showing {len(items)}):", ""]
    for item in items:
        lines.append(f"- h{item.get('level', '?')}: {item.get('text', '')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "sse":
        mcp.settings.host = os.environ.get("MCP_HOST", "0.0.0.0")
        mcp.settings.port = int(os.environ.get("MCP_PORT", "3000"))
    mcp.run(transport=transport)
