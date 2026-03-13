FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt ./

# Single layer: install everything → aggressive cleanup
RUN pip install --root-user-action=ignore -r requirements.txt \
    && playwright install --with-deps chromium \
    # ── Remove full Chromium (602MB) — only headless_shell is used ──
    && rm -rf /root/.cache/ms-playwright/chromium-*/ \
    && rm -rf /root/.cache/ms-playwright/ffmpeg-*/ \
    # ── Remove pip cache, apt caches, docs, locales ──
    && apt-get clean \
    && rm -rf /root/.cache/pip \
    && rm -rf \
        /var/lib/apt/lists/* \
        /var/cache/apt/* \
        /tmp/* \
        /usr/share/doc/* \
        /usr/share/man/* \
        /usr/share/locale/* \
        /usr/share/info/* \
        /usr/share/lintian/*

EXPOSE 3000

CMD ["python", "server.py"]
