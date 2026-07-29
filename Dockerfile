FROM python:3.10-slim

WORKDIR /app

RUN pip install uv

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY src/ src/

ENV PYTHONUNBUFFERED=1

# No HEALTHCHECK here: this image is shared by api, mcp, and web (each overrides
# CMD via docker-compose), and each listens on a different port with a different
# health surface (mcp speaks MCP's session-based protocol, not plain HTTP GET).
# A single hardcoded check here would be wrong for at least two of the three -
# per-service healthcheck: blocks live in docker-compose.yml instead.

CMD ["uv", "run", "uvicorn", "docdb_id.api.server:app", "--host", "0.0.0.0", "--port", "8000"]
