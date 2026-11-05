FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project
COPY src ./src
RUN uv sync --locked --no-dev --no-editable
RUN groupadd --system vault && useradd --system --gid vault vault && mkdir /app/.local && chown vault:vault /app/.local
USER vault
EXPOSE 8765
CMD ["/app/.venv/bin/cvault", "serve", "--host", "0.0.0.0"]
