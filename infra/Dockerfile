# Reproducible container for the accounts-payable processing agent.
#
# Two stages. The first resolves the locked dependency set; the second carries only the
# virtual environment and the source, so the image does not ship a package manager or a
# build cache. The result runs the same code the tests ran against, because both install
# from uv.lock rather than from a version range.

FROM python:3.12-slim AS builder

# uv is pinned. An unpinned installer would make the image non-reproducible even with a
# locked dependency set, because a future uv could resolve the lock differently.
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv

WORKDIR /build
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# Dependencies first, source second, so a source change does not re-resolve dependencies.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-dev

COPY src/ ./src/
RUN uv sync --frozen --no-dev


FROM python:3.12-slim AS runtime

# A non-root user with no login shell. The service writes only to /app/data, which is the
# single mount point that needs to be writable.
RUN useradd --create-home --shell /usr/sbin/nologin --uid 10001 apagent

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AP_LLM_PROVIDER=fake \
    AP_CORPUS_DIR=/app/finance_rag_corpus \
    AP_INDEX_DIR=/app/data/index \
    AP_DB_PATH=/app/data/runtime/ap_agent.db

COPY --from=builder /build/.venv /app/.venv
COPY src/ /app/src/
COPY finance_rag_corpus/ /app/finance_rag_corpus/
COPY fixtures/ /app/fixtures/
COPY tests/eval/retrieval_golden.json /app/tests/eval/retrieval_golden.json

RUN mkdir -p /app/data/index /app/data/runtime && chown -R apagent:apagent /app/data
USER apagent

# Build the index at image build time so a container starts ready to serve. It is rebuilt
# automatically if the corpus is mounted over with different content.
RUN ap-agent ingest

EXPOSE 8000

# The service has no authentication, so the container binds 0.0.0.0 on the assumption that
# network exposure is controlled outside it. Do not publish this port to an untrusted network.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=4).status == 200 else 1)"

CMD ["ap-agent", "serve", "--host", "0.0.0.0", "--port", "8000"]
