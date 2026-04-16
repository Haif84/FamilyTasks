FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash appuser

COPY pyproject.toml README.md ./
COPY src ./src

ARG CACHEBUST=0
RUN echo "cachebust=${CACHEBUST}" \
    && pip install --upgrade pip setuptools wheel \
    && pip install .

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Default path inside container (override via env)
ENV DB_PATH=/app/data/family_tasks.sqlite3

VOLUME ["/app/data"]

# Root only for chown on volume; process drops to appuser in entrypoint.
USER root
ENTRYPOINT ["/docker-entrypoint.sh"]
CMD ["python", "-m", "family_tasks_bot.main"]
