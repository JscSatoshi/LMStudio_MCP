#!/usr/bin/env python3
"""
deploy.py — Launch SearXNG + MCP Server via Docker Compose.
"""

import argparse
import os
import select
import shutil
import subprocess
import sys
import termios
import threading
import time
import tty
import urllib.request
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
ROOT         = Path(__file__).parent.resolve()
COMPOSE_FILE = ROOT / "docker-compose.yml"
ENV_FILE     = ROOT / ".env"

SEARXNG_PORT = 8081
MCP_PORT     = 3000
HEALTH_WAIT  = 60   # seconds
HEALTH_TICK  = 3    # seconds between checks

# ---------------------------------------------------------------------------
# Pretty output
# ---------------------------------------------------------------------------
_C = {
    "cyan":   "\033[0;36m",
    "green":  "\033[0;32m",
    "yellow": "\033[1;33m",
    "red":    "\033[0;31m",
    "reset":  "\033[0m",
}

def _c(color: str, text: str) -> str:
    return f"{_C[color]}{text}{_C['reset']}"

def step(n: int, total: int, msg: str) -> None:
    print(f"{_c('cyan', f'[{n}/{total}]')} {msg}")

def ok(msg: str)   -> None: print(f"{_c('green',  '[  OK  ]')} {msg}")
def warn(msg: str) -> None: print(f"{_c('yellow', '[ WARN ]')} {msg}")
def die(msg: str)  -> None:
    print(f"{_c('red', '[ ERR  ]')} {msg}", file=sys.stderr)
    sys.exit(1)

# ---------------------------------------------------------------------------
# Docker helpers
# ---------------------------------------------------------------------------

def find_compose() -> list[str]:
    """Detect 'docker compose' (plugin) or 'docker-compose' (standalone)."""
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode == 0:
        return ["docker", "compose"]
    if shutil.which("docker-compose"):
        return ["docker-compose"]
    return []


def compose(*args: str, dc: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a docker compose subcommand, streaming output to the terminal."""
    return subprocess.run(
        dc + ["-f", str(COMPOSE_FILE)] + list(args),
        check=check,
    )


def compose_with_env(*args: str, dc: list[str], env: dict[str, str], check: bool = True) -> subprocess.CompletedProcess:
    """Run docker compose with environment overrides."""
    merged_env = dict(os.environ)
    merged_env.update(env)
    return subprocess.run(
        dc + ["-f", str(COMPOSE_FILE)] + list(args),
        check=check,
        env=merged_env,
    )


def docker_context() -> str:
    """Return the current Docker context name, if available."""
    result = subprocess.run(
        ["docker", "context", "show"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def ensure_docker_daemon() -> None:
    """Fail fast when the Docker daemon is not reachable."""
    result = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return

    context = docker_context() or "unknown"
    detail = (result.stderr or result.stdout).strip()
    hint = "Start Docker Desktop or the active Docker daemon, then retry."

    if context == "colima":
        hint = "Current Docker context is 'colima'. Run 'colima start' and retry."

    die(
        "Docker daemon is unavailable.\n"
        f"Active context: {context}\n"
        f"{detail}\n"
        f"{hint}"
    )


def image_exists(name: str) -> bool:
    return subprocess.run(
        ["docker", "image", "inspect", name],
        capture_output=True,
    ).returncode == 0


def is_reachable(url: str, timeout: int = 2) -> bool:
    try:
        urllib.request.urlopen(url, timeout=timeout)
        return True
    except Exception:
        return False

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def preflight(dc: list[str]) -> None:
    if not shutil.which("docker"):
        die("Docker not found. Install Docker Desktop and try again.")
    if not dc:
        die("Neither 'docker compose' nor 'docker-compose' found.")
    ensure_docker_daemon()
    if not ENV_FILE.exists():
        die(f".env not found at {ENV_FILE}\nCreate it with: SEARXNG_SECRET=<random-string>")

    secret = ""
    for line in ENV_FILE.read_text().splitlines():
        if line.startswith("SEARXNG_SECRET="):
            secret = line.split("=", 1)[1].strip()
    if not secret:
        die("SEARXNG_SECRET is missing or empty in .env")

    ok(f"Docker: {' '.join(dc)}  |  .env: OK")

# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def do_stop(dc: list[str]) -> None:
    print(f"\n{_c('yellow', 'Stopping containers...')}")
    compose("down", dc=dc)
    ok("All containers stopped.\n")


def do_pull(dc: list[str]) -> None:
    image = "searxng/searxng:latest"
    if image_exists(image):
        ok(f"{image} cached locally — skipping pull.")
    else:
        print(f"  {image} not found — pulling...")
        result = subprocess.run(["docker", "pull", image], check=False)
        if result.returncode != 0:
            warn(f"Pull failed for {image}. Docker will retry during 'up'.")


def do_build(dc: list[str]) -> None:
    print("  building MCP service images...")
    compose_with_env(
        "build",
        "mcp",
        dc=dc,
        env={"DOCKER_BUILDKIT": "0"},
    )


# Base images only needed at build time — safe to remove after build
_BASE_IMAGES = ("python:3.12-slim",)


def _cleanup_base_images() -> None:
    """Remove base images that are no longer needed after build."""
    for image in _BASE_IMAGES:
        if image_exists(image):
            result = subprocess.run(
                ["docker", "rmi", image],
                capture_output=True,
            )
            if result.returncode == 0:
                ok(f"Removed base image {image} to save disk space.")
            else:
                warn(f"Could not remove {image} (may still be in use).")


def do_launch(dc: list[str], detach: bool) -> None:
    cmd = ["--env-file", str(ENV_FILE), "up"]
    if detach:
        cmd.append("-d")
    compose(*cmd, dc=dc)


def do_health_check() -> tuple[bool, bool]:
    print()
    searxng_up = mcp_up = False
    elapsed = 0

    while elapsed < HEALTH_WAIT:
        if not searxng_up and is_reachable(f"http://localhost:{SEARXNG_PORT}/"):
            ok(f"SearXNG  →  http://localhost:{SEARXNG_PORT}")
            searxng_up = True

        if not mcp_up and is_reachable(f"http://localhost:{MCP_PORT}/sse"):
            ok(f"MCP      →  http://localhost:{MCP_PORT}/sse")
            mcp_up = True

        if searxng_up and mcp_up:
            break

        time.sleep(HEALTH_TICK)
        elapsed += HEALTH_TICK
        print(f"  waiting... {elapsed}s / {HEALTH_WAIT}s", end="\r", flush=True)

    print()  # clear the \r line
    return searxng_up, mcp_up


def do_summary(searxng_up: bool, mcp_up: bool) -> None:
    if not searxng_up:
        warn("SearXNG unreachable — run: docker logs searxng")
    if not mcp_up:
        warn("MCP unreachable — run: docker logs mcp")

    bar    = _c("green", "━" * 58)
    s_icon = _c("green", "●") if searxng_up else _c("yellow", "○")
    m_icon = _c("green", "●") if mcp_up     else _c("yellow", "○")

    print(f"\n{bar}")
    print(f"  {s_icon}  SearXNG UI  http://localhost:{SEARXNG_PORT}")
    print(f"  {m_icon}  MCP (SSE)   http://localhost:{MCP_PORT}/sse")
    print(f"\n  {_c('cyan', 'docker logs -f searxng')}    SearXNG logs")
    print(f"  {_c('cyan', 'docker logs -f mcp')}          MCP logs")
    print(f"  {_c('cyan', 'python3 deploy.py --stop')}    stop all")
    print(f"  {_c('cyan', 'python3 deploy.py --start')}   start again")
    print(f"{bar}\n")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _stream_logs(dc: list[str]) -> None:
    """Stream docker-compose logs; press Enter or Space to stop."""
    print(_c("cyan", "Streaming logs — press [Enter] or [Space] to stop\n"))
    proc = subprocess.Popen(
        dc + ["-f", str(COMPOSE_FILE), "logs", "-f"],
        stdin=subprocess.DEVNULL,
    )

    stop = threading.Event()

    def _watch_key():
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)          # cbreak: keys available immediately, but \n still works
            while not stop.is_set():
                if select.select([sys.stdin], [], [], 0.2)[0]:
                    ch = sys.stdin.read(1)
                    if ch in ("\r", "\n", " "):
                        stop.set()
                        break
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    t = threading.Thread(target=_watch_key, daemon=True)
    t.start()

    try:
        while proc.poll() is None and not stop.is_set():
            time.sleep(0.2)
    finally:
        stop.set()
        proc.terminate()
        proc.wait()
    print()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Deploy SearXNG + MCP Server",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 deploy.py --start      start containers in background\n"
            "  python3 deploy.py --stop       stop all containers\n"
            "  python3 deploy.py --logs       stream logs after startup\n"
        ),
    )
    parser.add_argument("--start", action="store_true", help="Start containers in background")
    parser.add_argument("--stop",  action="store_true", help="Stop and remove all containers")
    parser.add_argument("--logs",  action="store_true", help="Stream container logs after startup")
    args = parser.parse_args()

    dc = find_compose()

    # --- Stop ---------------------------------------------------------------
    if args.stop:
        preflight(dc)
        do_stop(dc)
        return

    # --- Logs (standalone) --------------------------------------------------
    if args.logs and not args.start:
        preflight(dc)
        _stream_logs(dc)
        return

    # --- Require an explicit flag -------------------------------------------
    if not args.start:
        parser.print_help()
        return

    # --- Launch -------------------------------------------------------------
    step(1, 4, "Preflight checks")
    preflight(dc)

    step(2, 4, "Preparing images")
    do_pull(dc)

    step(3, 4, "Building MCP images")
    do_build(dc)
    _cleanup_base_images()

    step(4, 4, "Launching containers")
    do_launch(dc, detach=True)

    # --- Post-launch --------------------------------------------------------
    searxng_up, mcp_up = do_health_check()
    do_summary(searxng_up, mcp_up)

    if args.logs:
        _stream_logs(dc)

if __name__ == "__main__":
    main()
