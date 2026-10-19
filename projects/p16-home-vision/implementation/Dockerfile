# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME_VISION_ENVIRONMENT=production \
    HOME_VISION_DATABASE_URL=sqlite:////state/home-vision.db \
    HOME_VISION_DATA_ROOT=/state/data

RUN sed -i 's|deb.debian.org/debian-security|mirrors.aliyun.com/debian-security|g; s|deb.debian.org/debian|mirrors.aliyun.com/debian|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ARG P16_RELEASE_REVISION=dev-unpinned
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

LABEL org.opencontainers.image.revision="${P16_RELEASE_REVISION}" \
      io.tevriq.p16.release-revision="${P16_RELEASE_REVISION}"

ENV P16_RELEASE_REVISION=${P16_RELEASE_REVISION}

WORKDIR /srv/home-vision

COPY requirements.lock ./requirements.lock

# Keep dependency resolution independent from application source changes. The
# BuildKit cache survives code-only rebuilds without becoming an image layer.
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --requirement requirements.lock

COPY pyproject.toml alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY analysis_worker ./analysis_worker
COPY scripts ./scripts
COPY container-entrypoint.sh ./container-entrypoint.sh

RUN --mount=type=cache,target=/root/.cache/pip \
    PIP_INDEX_URL=${PIP_INDEX_URL} python -m pip install --no-deps . \
    && chmod +x /srv/home-vision/container-entrypoint.sh \
    && mkdir -p /state/data /archive/a /archive/b \
    && chown -R 10001:10001 /state /archive /srv/home-vision

USER 10001:10001

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).read()" || exit 1

ENTRYPOINT ["/srv/home-vision/container-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
