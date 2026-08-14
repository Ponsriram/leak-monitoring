# Build context is services/intel.
#
#   docker build -f services/intel/Dockerfile services/intel

FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

# uv from the official distroless image — faster and more reproducible than pip installing it.
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv

WORKDIR /app

# Dependency layer first: lockfile changes are rare, source changes are constant.
#
# The `browser` extra is included because 81 of the 83 entries in sources.yaml declare
# `collector: browser`. Without it the worker imports fine and then dies at fetch time with
# ModuleNotFoundError on all but two sources — the image would be nearly useless.
COPY pyproject.toml uv.lock* ./
RUN uv sync --no-install-project --no-dev --extra browser

# Firefox plus its system libraries. --with-deps pulls the shared objects the browser needs,
# which a slim image does not carry.
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN /opt/venv/bin/playwright install --with-deps firefox \
    && chmod -R a+rX /opt/playwright

COPY intel ./intel
COPY sources.yaml ./sources.yaml
RUN uv sync --no-dev --extra browser

ENV PATH="/opt/venv/bin:$PATH"

# Non-root. The worker needs no write access to anything in the image.
RUN useradd --create-home --uid 10001 intel && chown -R intel:intel /app
USER intel

# Default to the arq worker: this image exists to run scheduled crawls. Override the command
# to use the CLI ad hoc, e.g. `docker compose run --rm worker intel sources list --all`.
CMD ["arq", "intel.tasks.WorkerSettings"]
