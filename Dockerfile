FROM ghcr.io/astral-sh/uv:0.9.5 AS uv

FROM python:3.12-slim

WORKDIR /app
COPY --from=uv /uv /uvx /bin/
COPY pyproject.toml uv.lock alembic.ini ./
COPY src ./src
RUN uv sync --frozen --no-dev \
    && groupadd --gid 10001 cda \
    && useradd --uid 10001 --gid cda --create-home cda \
    && chown -R cda:cda /app

ENV PATH="/app/.venv/bin:$PATH"
USER cda
