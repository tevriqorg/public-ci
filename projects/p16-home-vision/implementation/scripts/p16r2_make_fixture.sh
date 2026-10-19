#!/usr/bin/env bash
set -euo pipefail

ASSET_ROOT="${HOME_VISION_R2_ASSET_ROOT:-$HOME/P16R2/assets}"
FIXTURE_ROOT="${HOME_VISION_R2_FIXTURE_ROOT:-$HOME/P16R2/fixtures}"
RAW="$FIXTURE_ROOT/p16r2-smoke.mkv"
ASSET_ID="smoke-$(date +%Y%m%d-%H%M%S)"

mkdir -p "$ASSET_ROOT" "$FIXTURE_ROOT"

ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i 'testsrc2=size=640x360:rate=15:duration=8' \
  -f lavfi -i 'sine=frequency=880:sample_rate=8000:duration=8' \
  -c:v libx265 -preset ultrafast \
  -x265-params 'pools=1:frame-threads=1:log-level=error' \
  -pix_fmt yuv420p \
  -c:a pcm_alaw \
  -shortest \
  "$RAW"

p16-media-derive \
  --raw "$RAW" \
  --asset-root "$ASSET_ROOT" \
  --asset-id "$ASSET_ID" \
  --camera 'PoC 测试摄像头' \
  --started-at "$(date -Iseconds)"

echo "RAW retained: $RAW"
echo "Asset created: $ASSET_ROOT/$ASSET_ID"
