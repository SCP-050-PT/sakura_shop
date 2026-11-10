FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY alembic.ini .
COPY migrations ./migrations
COPY assets ./assets
COPY app ./app

RUN useradd --create-home --uid 1000 bot && mkdir -p /app/data && chown bot:bot /app/data
USER bot

VOLUME ["/app/data"]
CMD ["python", "-m", "app"]
