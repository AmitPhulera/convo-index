# The public playground: convo-index serving fictional conversations, read-only.
#   docker build -t convo-index-playground . && docker run --rm -p 9753:9753 convo-index-playground
# Everything the server reads is baked here (database, embedding model, DuckDB's sqlite
# extension), so the container runs with a read-only file system and no network at run time.

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY convo_index ./convo_index
RUN uv sync --frozen --no-dev --no-editable
COPY demo ./demo
COPY scripts/make_demo_data.py scripts/demo_writers.py ./scripts/
# The model is fetched into the same path the runtime config names; extensions are tied to the
# exact DuckDB version, so they are installed with the venv's own DuckDB.
RUN .venv/bin/python scripts/make_demo_data.py --out /srv/playground --models /srv/playground/models \
 && .venv/bin/python -c "from convo_index.analytics import install_duckdb_extensions as i; i('/srv/playground/duckdb')" \
 && rm -rf /srv/playground/models/.locks

FROM python:3.13-slim
COPY --from=build /app/.venv /app/.venv
COPY --from=build /srv/playground /srv/playground
ENV PATH=/app/.venv/bin:$PATH \
    CONVO_INDEX_CONFIG=/srv/playground/config.toml \
    CONVO_INDEX_DB=/srv/playground/convos.db \
    CONVO_INDEX_DEMO=1 \
    CONVO_INDEX_DUCKDB_EXTENSIONS=/srv/playground/duckdb \
    HF_HUB_OFFLINE=1 \
    HOME=/tmp \
    PYTHONUNBUFFERED=1
USER nobody
EXPOSE 9753
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9753/api/sources', timeout=4)"]
CMD ["convo-index", "serve"]
