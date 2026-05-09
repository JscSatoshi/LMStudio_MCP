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
        "You have web tools. Follow this IF-THEN routing strictly:\n"
        "1) IF user asks for a visual (screenshot/capture/show me/look like), THEN call screenshot(url).\n"
        "2) IF user gives a specific URL and wants page content, THEN call read_page(url, mode='full').\n"
        "3) IF user gives a specific URL and wants structure, THEN call read_page with mode='links' or mode='headlines'.\n"
        "4) IF user asks for text from part of a page, THEN call read_page(url, mode='text', selector='...').\n"
        "5) IF user asks a web question without a URL, THEN call search(query) first.\n"
        "6) IF search snippets are insufficient for final answer, THEN escalate once to deep_search(query).\n"
        "7) IF deep_search still lacks a key fact, THEN call read_page(url, mode='full') on one best candidate URL.\n\n"
        "TOOLS:\n"
        "- search(query, categories, language, time_range): fast snippets for discovery.\n"
        "- deep_search(query,...): slower full-page reading for multi-source synthesis.\n"
        "- read_page(url, mode, selector): unified single-page reader.\n"
        "- navigate(url, format): direct text/html fetch when explicit raw HTML is required.\n"
        "- screenshot(url): visual output.\n\n"
        "QUERY TUNING:\n"
        "- Recent topics (news, prices, releases): time_range='week' or 'month'.\n"
        "- Use categories='news' for current events, 'it' for programming, 'science' for academic.\n"
        "- Keep language='auto' unless user explicitly needs another language.\n\n"
        "CALL BUDGET RULES:\n"
        "- Prefer one good call over many shallow calls.\n"
        "- Never repeat identical failed/empty calls; change at least one parameter.\n"
        "- If search returns 0: broaden query -> time_range='' -> categories='general' -> language='all'.\n"
        "- On tool error (prefix ERROR:), adjust inputs instead of blind retry.\n"
        "- Stop calling tools once evidence is sufficient to answer."
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
        return f"ERROR: search failed: {err}"

    if not results:
        unresponsive = data.get("unresponsive_engines", [])
        hint_lines = ["No search results found."]
        if unresponsive:
            engines = ", ".join(e[0] for e in unresponsive[:5])
            hint_lines.append(f"Unresponsive engines: {engines}")
        hint_lines.append(
            "Hint: try a broader query, time_range='', or language='all'. "
            "Do not retry with identical arguments."
        )
        return "\n".join(hint_lines)

    lines = [f"=== Search: '{query}' — {len(results)} results ===\n"]
    for i, r in enumerate(results, 1):
        title     = r.get("title", "").strip()
        url       = r.get("url", "")
        snippet   = r.get("content", "").strip()
        published = r.get("published", "")
        lines.append(f"[{i}] {title}")
        lines.append(f"    {url}")
        if published:
            lines.append(f"    published: {published}")
        if snippet:
            lines.append(f"    {snippet[:300]}")
        lines.append("")
    lines.append(
        "Hint: if snippets are not enough to answer, call deep_search with the same query "
        "or navigate(url) on a top result. Do not call search again with the same arguments."
    )
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
        return f"ERROR: deep_search failed: {err}"

    if not pages:
        return (
            "No search results found.\n"
            "Hint: try a broader query, time_range='', or language='all'. "
            "Do not retry with identical arguments."
        )

    failed = sum(
        1 for p in pages if p.get("content", "").startswith(("[fetch error:", "[blocked url:"))
    )
    lines = [f"=== Deep Search: '{query}' - reading {len(pages)} pages ===", ""]
    for i, page in enumerate(pages, 1):
        lines.append(f"--- [{i}] {page.get('title', '')}")
        lines.append(f"    {page.get('url', '')}")
        lines.append("")
        lines.append(page.get("content", ""))
        lines.append("")
    if failed:
        lines.append(
            f"Hint: {failed}/{len(pages)} pages failed to load. "
            "Try navigate(url) on a specific URL or rerun with a different query."
        )
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
        return f"ERROR: navigate({url}): {result['error']}"
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
        raise RuntimeError(f"ERROR: screenshot({url}): {exc}") from exc


@mcp.tool()
async def read_page(
    url: str,
    mode: str = "text",
    selector: str = "body",
    wait_until: str = "domcontentloaded",
) -> str:
    """
    Unified page reader for small-model routing.

    Args:
        url:        The URL to read.
        mode:       One of: 'links', 'text', 'headlines', 'full'.
        selector:   CSS selector for mode='text'.
        wait_until: 'load', 'domcontentloaded', or 'networkidle'.
    """
    mode = (mode or "text").strip().lower()

    if mode == "links":
        result = await core.extract_links(url=url, wait_until=wait_until)
        if "error" in result:
            return f"ERROR: read_page({url}, mode='links'): {result['error']}"
        links = result.get("links", [])
        if not links:
            return "No links found on this page."
        lines = []
        for link in links[:200]:
            text = link.get("text", "").replace("\n", " ").strip()
            href = link.get("href", "")
            lines.append(f"- [{text}]({href})" if text else f"- {href}")
        truncated = len(links) > len(lines)
        header = f"Found {len(links)} links (showing {len(lines)}{', truncated' if truncated else ''}):"
        return header + "\n" + "\n".join(lines)

    if mode == "headlines":
        result = await core.headlines(url=url, wait_until=wait_until)
        if "error" in result:
            return f"ERROR: read_page({url}, mode='headlines'): {result['error']}"
        items = result.get("headlines", [])
        if not items:
            return "No headlines found on this page."
        lines = [f"Found {result.get('count', len(items))} headlines (showing {len(items)}):", ""]
        for item in items:
            lines.append(f"- h{item.get('level', '?')}: {item.get('text', '')}")
        return "\n".join(lines)

    if mode == "full":
        result = await core.navigate(url=url, format="text", wait_until=wait_until)
        if "error" in result:
            return f"ERROR: read_page({url}, mode='full'): {result['error']}"
        return result.get("content", "")

    if mode == "text":
        result = await core.extract_text(url=url, selector=selector, wait_until=wait_until)
        if "error" in result:
            return (
                f"ERROR: read_page({url}, mode='text', selector={selector!r}): "
                f"{result['error']}"
            )
        return result.get("content", "")

    return (
        "ERROR: read_page invalid mode. "
        "Use one of: links, text, headlines, full."
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport in ("sse", "streamable-http"):
        mcp.settings.host = os.environ.get("MCP_HOST", "0.0.0.0")
        mcp.settings.port = int(os.environ.get("MCP_PORT", "3000"))
    mcp.run(transport=transport)
