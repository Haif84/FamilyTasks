FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN useradd --create-home --shell /bin/bash appuser

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --upgrade pip setuptools wheel \
    && pip install .

USER appuser

# Default path inside container (override via env)
ENV DB_PATH=/app/data/family_tasks.sqlite3

VOLUME ["/app/data"]

CMD ["python", "-m", "family_tasks_bot.main"]
