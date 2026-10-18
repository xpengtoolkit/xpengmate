# The standalone XPENG engine: XPENG auth + MQTT telemetry in, the
# Tesla-compatible provider API out. It shares no code with xpengmate's Elixir
# image; the two talk over HTTP on the compose network.
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY xpeng_engine ./xpeng_engine

# The three mTLS files, the tenant and the token file are mounted, never baked.
# /data is the single mount point: certs/, secrets/ and state/ below it.
VOLUME ["/data"]

EXPOSE 8201

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8201/healthz',timeout=4).status==200 else 1)"

CMD ["python", "-m", "xpeng_engine"]
