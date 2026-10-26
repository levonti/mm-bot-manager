FROM python:3.14-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    PYTHONDONTWRITEBYTECODE=1

RUN pip install --no-cache-dir uv==0.11.7

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project --extra postgres

COPY src ./src
RUN uv sync --frozen --no-dev --extra postgres


FROM python:3.14-slim AS runtime

ARG APP_VERSION=unknown
ARG MM_BUILD_REVISION=unknown
ENV MM_BUILD_REVISION=$MM_BUILD_REVISION
LABEL org.opencontainers.image.version=$APP_VERSION \
      org.opencontainers.image.revision=$MM_BUILD_REVISION

# OS runtime packages; psycopg[binary] supplies its own libpq.
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && python -m pip uninstall --yes pip

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

RUN useradd --system --uid 1000 --home /app mmbot
USER mmbot

CMD ["python", "-m", "mm_bot_manager"]
