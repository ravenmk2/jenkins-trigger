# ---- Build stage: resolve dependencies into a venv with uv ----
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

WORKDIR /app

# Copy instead of hardlinking from the uv cache, and pre-compile bytecode
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# Install dependencies first to leverage build cache
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

COPY src ./src
RUN uv sync --locked --no-dev

# ---- Runtime stage: plain Python, no uv / build tooling ----
FROM python:3.14-slim-bookworm

LABEL org.opencontainers.image.title="jenkins-trigger" \
      org.opencontainers.image.description="Trigger Jenkins jobs from cron schedules and GitLab push webhooks with stage-based orchestration" \
      org.opencontainers.image.source="https://github.com/ravenmk2/jenkins-trigger" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

COPY --from=builder /app/.venv ./.venv
COPY src ./src

ENV PATH="/app/.venv/bin:$PATH"

# Run as a dedicated non-root user; data dir writable by that user
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data && chown app:app /app/data

# 运行数据(分支 commit 记录)持久化卷
VOLUME ["/app/data"]

USER app

EXPOSE 8080 8081

# Config (with secrets) is not baked into the image; mount it at /app/config
CMD ["python", "-m", "jenkins_trigger"]
