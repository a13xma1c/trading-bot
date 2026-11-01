# syntax=docker/dockerfile:1.7
#
# Trading Bot container image.
#
#   docker build -t trading-bot .
#   docker run -p 8080:8080 -v trading-bot-data:/data \
#       -e TRADING_BOT_WEB_PASSWORD=change-me -e TRADING_BOT__DATA__PROVIDER=demo trading-bot
#
# Stages: builder (dependencies via uv, locked) -> runtime (slim, non-root) ; test (dev tools) for CI.

ARG PYTHON_VERSION=3.13

# ------------------------------------------------------------------ builder
FROM python:${PYTHON_VERSION}-slim AS builder
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src
COPY pyproject.toml uv.lock README.md ./
# Dependencies first (cached layer), project last.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --no-editable
COPY trading_bot ./trading_bot
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# --------------------------------------------------------------------- test
# Dev tooling for CI (unit/API/scalability tests without a browser).
FROM builder AS test
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --extra dev --no-editable
COPY tests ./tests
COPY strategies ./strategies
ENV PATH=/opt/venv/bin:$PATH
CMD ["pytest", "-q", "--ignore=tests/test_web_e2e.py"]

# ------------------------------------------------------------------ runtime
FROM python:${PYTHON_VERSION}-slim AS runtime
LABEL org.opencontainers.image.title="Trading Bot" \
      org.opencontainers.image.description="Real-time trading signal bot with a web UI" \
      org.opencontainers.image.source="https://github.com/example/trading-bot"
RUN groupadd --gid 1000 bot && useradd --uid 1000 --gid bot --create-home --shell /usr/sbin/nologin bot \
    && mkdir -p /data && chown bot:bot /data
WORKDIR /app
COPY --from=builder --chown=bot:bot /opt/venv /opt/venv
COPY --chown=bot:bot strategies ./strategies
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TRADING_BOT_CONFIG=/data/config.json \
    TRADING_BOT__WEB__HOST=0.0.0.0 \
    TRADING_BOT__WEB__PORT=8080 \
    TRADING_BOT__WEB__OPEN_BROWSER=false \
    TRADING_BOT__PLUGINS_DIR=/app/strategies
VOLUME ["/data"]
EXPOSE 8080
USER bot
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/api/health', timeout=4).status == 200 else 1)"
ENTRYPOINT ["trading-bot"]
CMD ["serve", "--no-browser"]
