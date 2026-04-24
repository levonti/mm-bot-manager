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

# libpq is required by psycopg[binary] at runtime
RUN apt-get update && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src

RUN useradd --system --uid 1000 --home /app mmbot
USER mmbot

CMD ["python", "-m", "mm_bot_manager"]
