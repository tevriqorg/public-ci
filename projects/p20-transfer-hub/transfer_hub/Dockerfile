FROM node:24-alpine AS web-build

WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund
COPY web/ ./
RUN npm run build

FROM python:3.12-slim

ARG YTDLP_VERSION=2026.08.19
ARG GALLERY_DL_VERSION=1.32.12
ARG NM3U8_VERSION=0.6.0-beta
ARG NM3U8_FILE=N_m3u8DL-RE_v0.6.0-beta_linux-x64_20260629.tar.gz
ARG NM3U8_SHA256=2ebcd450594a08c0de0bcd502a80c5d3f89a702a0fa6604199527bb8d6eee4db

RUN apt-get update \
 && apt-get install -y --no-install-recommends aria2 p7zip-full ffmpeg curl ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "yt-dlp==${YTDLP_VERSION}" "gallery-dl==${GALLERY_DL_VERSION}"

RUN curl -fsSL -o /tmp/nm3u8.tar.gz \
    "https://github.com/nilaoda/N_m3u8DL-RE/releases/download/v${NM3U8_VERSION}/${NM3U8_FILE}" \
 && echo "${NM3U8_SHA256}  /tmp/nm3u8.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/nm3u8.tar.gz -C /usr/local/bin \
 && chmod +x /usr/local/bin/N_m3u8DL-RE \
 && rm /tmp/nm3u8.tar.gz

COPY vendor/uav/ /opt/p20-uav/
RUN pip install --no-cache-dir -r /opt/p20-uav/requirements.txt

WORKDIR /app
COPY pyproject.toml /app/
COPY p20_receive.py p20_cmcloud_e2ee.py /app/
COPY transfer_hub /app/transfer_hub
COPY --from=web-build /web/dist /app/transfer_hub/web_dist
RUN pip install --no-cache-dir '.[mcp]'

EXPOSE 17610
CMD ["python", "-m", "transfer_hub"]
