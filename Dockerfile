FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DB_PATH=/app/data/bot.db

WORKDIR /app

COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY config.py ./
COPY analysis ./analysis
COPY bot ./bot
COPY collectors ./collectors
COPY storage ./storage

RUN useradd --create-home --uid 1000 bot && mkdir -p /app/data && chown bot /app/data
USER bot
VOLUME ["/app/data"]

# Healthy = the database file was written to in the last 10 minutes (the collectors write every few seconds).
HEALTHCHECK --interval=5m --timeout=10s --start-period=2m \
    CMD python -c "import os,time,sys; p=os.environ['DB_PATH']; sys.exit(0 if time.time()-max(os.path.getmtime(f) for f in (p, p+'-wal') if os.path.exists(f)) < 600 else 1)"

CMD ["python", "-m", "bot.main"]
