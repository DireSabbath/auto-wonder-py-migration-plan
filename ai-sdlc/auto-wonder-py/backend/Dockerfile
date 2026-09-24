FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.18 /uv /uvx /bin/
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 7002
CMD ["autowonder-serve"]
