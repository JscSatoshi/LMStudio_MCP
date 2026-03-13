<div align="center">

# 🔍 LMStudio MCP

**Docker-based MCP server for LM Studio**
*Web search + headless browser in a single service*

![Docker](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)
![MCP](https://img.shields.io/badge/MCP-SSE-8A2BE2)
![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)
![Playwright](https://img.shields.io/badge/Playwright-Chromium-2EAD33?logo=playwright&logoColor=white)

</div>

---

## 🏗️ Architecture

```mermaid
flowchart TD
    LMS["🖥️ LM Studio (your machine)"]
    LMS <-->|"SSE :3000"| MCP

    subgraph Docker["🐳 Docker"]
        MCP["⚡ MCP Server :3000"]
        SearX["🔎 SearXNG :8081"]
        Chrome["🌐 Chromium headless"]
        MCP -->|search| SearX
        MCP -->|browse| Chrome
    end

    style Docker fill:#1e1e2e,stroke:#89b4fa,color:#cdd6f4
    style MCP fill:#313244,stroke:#a6e3a1,color:#a6e3a1
    style SearX fill:#313244,stroke:#f9e2af,color:#f9e2af
    style Chrome fill:#313244,stroke:#89b4fa,color:#89b4fa
    style LMS fill:#313244,stroke:#cba6f7,color:#cba6f7
```

| Service | Port | Description |
|:--------|:----:|:------------|
| 🔎 SearXNG | `8081` | Private search engine instance |
| 🌐 MCP | `3000` | Unified MCP server — search + browser tools via SSE |

---

## 🚀 Quick Start

#### 1️⃣ Create `.env`

```bash
echo "SEARXNG_SECRET=$(openssl rand -hex 32)" > .env
```

#### 2️⃣ Deploy

```bash
python3 deploy.py --start
```

#### 3️⃣ Configure LM Studio

```json
{
  "mcpServers": {
    "web": {
      "url": "http://localhost:3000/sse"
    }
  }
}
```

> [!WARNING]
> **After restarting the MCP container**, disconnect and reconnect the MCP server in LM Studio to avoid `-32602 Invalid request parameters` session errors.

---

## 🛠️ MCP Tools

Endpoint: `localhost:3000/sse`

| Tool | Description |
|:-----|:------------|
| 🔎 `search` | **Default tool.** Query SearXNG → titles, URLs, snippets. Fast (~1s). |
| 📖 `deep_search` | Search → fetch full rendered page content with Playwright. Use when snippets aren't enough. |
| 🧭 `navigate` | Fetch a single URL — text (default) or raw HTML (`format='html'`). |
| 📸 `screenshot` | Capture a screenshot of a page (returned as image). |
| 🔗 `extract_links` | Extract all hyperlinks from a page. |
| ✂️ `extract_text` | Extract text from a specific CSS selector on a page. |

<details>
<summary>📋 <code>search</code> parameters</summary>

| Parameter | Default | Description |
|:----------|:-------:|:------------|
| `query` | — | Search query |
| `categories` | `general` | `general`, `news`, `science`, `images`, `videos`, `it`, etc. |
| `language` | `auto` | Language code (`en`, `zh`, …) or `auto` |
| `safe_search` | `0` | `0` off · `1` moderate · `2` strict |
| `max_results` | `10` | Number of results (1–20) |

</details>

<details>
<summary>📋 <code>deep_search</code> parameters</summary>

| Parameter | Default | Description |
|:----------|:-------:|:------------|
| `query` | — | Search query |
| `categories` | `general` | `general`, `news`, `science`, `images`, `videos`, `it`, etc. |
| `language` | `auto` | Language code (`en`, `zh`, …) or `auto` |
| `safe_search` | `0` | `0` off · `1` moderate · `2` strict |
| `max_results` | `3` | Pages to fetch (1–5). Higher = richer but slower. |

</details>

---

## 📦 Commands

```bash
python3 deploy.py --start          # 🟢 Build images, start containers
python3 deploy.py --stop           # 🔴 Stop and remove containers
python3 deploy.py --logs           # 📜 Stream logs (Enter/Space to stop)
python3 deploy.py --start --logs   # 🟢📜 Start + stream logs
```

---

## 📜 View Logs

```bash
docker logs -f searxng    # SearXNG engine
docker logs -f mcp        # MCP server
```

---

## 🔄 Update server.py without rebuilding

> [!TIP]
> `server.py` is mounted as a volume — code changes take effect with a simple restart, no rebuild needed.

```bash
docker-compose restart mcp
```

Only rebuild when `requirements.txt` changes:

```bash
python3 deploy.py --start
```

---

## ⚙️ Environment Variables

| Variable | Default | Description |
|:---------|:-------:|:------------|
| `SEARXNG_URL` | `http://searxng:8080` | Internal SearXNG endpoint |
| `SEARXNG_TIMEOUT` | `15` | HTTP timeout (seconds) |
| `PAGE_TIMEOUT` | `10000` | Playwright navigation timeout (ms) |
| `FETCH_CONCURRENCY` | `8` | Parallel page fetches in `deep_search` |

> The MCP container is configured with `shm_size: 512m` to give Chromium enough shared memory. The Docker default (64 MB) causes renderer crashes.

## Project Structure

```
├── deploy.py              # Deployment script
├── docker-compose.yml     # Container orchestration
├── .env                   # SEARXNG_SECRET (create manually)
├── mcp/
│   ├── server.py          # Unified MCP server (SearXNG + Playwright)
│   ├── requirements.txt
│   ├── Dockerfile
│   └── .dockerignore
└── searxng/
    └── settings.yml       # SearXNG engine configuration
```

## Requirements

- Docker
- Docker Compose
- Python 3 (for `deploy.py` only)
