# ---- Build stage: resolve dependencies into a venv with uv ----
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

WORKDIR /app

# Copy instead of hardlinking from the uv cache, and pre-compile bytecode
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

# Install dependencies only; the project itself stays out of the venv so that
# code-only changes leave this layer (and the runtime .venv layer) untouched
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# ---- Runtime stage: plain Python, no uv / build tooling ----
FROM python:3.14-slim-bookworm

LABEL org.opencontainers.image.title="jenkins-trigger" \
      org.opencontainers.image.description="Trigger Jenkins jobs from cron schedules and GitLab push webhooks with stage-based orchestration" \
      org.opencontainers.image.source="https://github.com/ravenmk2/jenkins-trigger" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

# Create the user and data dir before COPY: this layer is independent of
# code/dependencies, so source changes never invalidate it.
# Never RUN chown -R after COPY: re-owning files from a previous layer
# copies the entire .venv into the new layer.
RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
    && mkdir -p /app/data && chown app:app /app/data

# Keep .venv / src root-owned; app only needs read+execute, which also stops
# the runtime process from tampering with code and dependencies
COPY --from=builder /app/.venv ./.venv
COPY src ./src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app/src"

# Persistent volume for runtime data (branch commit records)
VOLUME ["/app/data"]

USER app

EXPOSE 8080 8081

# Config (with secrets) is not baked into the image; mount it at /app/config
CMD ["python", "-m", "jenkins_trigger"]
