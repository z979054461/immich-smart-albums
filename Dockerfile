# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

LABEL org.opencontainers.image.title="Immich Smart Albums service" \
      org.opencontainers.image.description="Scheduled, configuration-driven Immich albums" \
      org.opencontainers.image.licenses="GPL-3.0-only"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SMART_ALBUMS_CONFIG=/config/config.yaml \
    SMART_ALBUMS_STATE_DIR=/data \
    TZ=Asia/Shanghai

RUN groupadd --gid 1000 smartalbums \
    && useradd --uid 1000 --gid 1000 --no-create-home --shell /usr/sbin/nologin smartalbums \
    && mkdir -p /app /config /data \
    && chown 1000:1000 /data
WORKDIR /app
COPY requirements-service.txt ./
RUN pip install --no-cache-dir -r requirements-service.txt
COPY smart_albums/ ./smart_albums/
COPY LICENSE ./LICENSE
USER 1000:1000
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD ["python", "-m", "smart_albums", "healthcheck"]
ENTRYPOINT ["python", "-m", "smart_albums"]
