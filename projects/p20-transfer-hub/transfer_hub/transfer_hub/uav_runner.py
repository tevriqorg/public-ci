"""One job per process; upstream remains unmodified."""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from .uav import candidates


def main() -> int:
    config_path = Path(sys.argv[1])
    config = json.loads(config_path.read_text())
    out = Path(config["out_dir"])
    os.environ["APPDATA"] = str(out / ".uav-state")
    sys.path.insert(0, os.getenv("P20_UAV_SOURCE", "/opt/p20-uav/src"))
    protocol = sys.stdout

    def emit(kind: str, **data) -> None:
        protocol.write("\nP20_UAV " + json.dumps({"type": kind, **data}, ensure_ascii=False) + "\n")
        protocol.flush()

    last = 0.0

    def progress(done, total, speed):
        nonlocal last
        now = time.monotonic()
        if now - last < 0.8 and done != total:
            return
        last = now
        emit("progress", percent=min(99.0, done / max(1, total) * 100), speed=max(0, int(speed)), eta=None)

    with contextlib.redirect_stdout(sys.stderr):
        from uav_downloader.sites import CreateSite
        from uav_downloader.sites.base import set_resolution_pref
        set_resolution_pref(os.getenv("P20_UAV_RESOLUTION", "highest"))
        errors = []
        for index, url in enumerate(candidates(config["source"]), 1):
            emit("message", message=f"UAV 正在解析来源 {index}")
            site = CreateSite(url, str(out), silence=True, max_workers=4)
            if site is None or not site.is_url_vaildate():
                errors.append(str(getattr(site, "_last_error", "不支持的链接")))
                continue
            # Apply explicit task authentication to media requests without writing it
            # into logs or process arguments. Page auth is not auto-imported.
            site._extra_headers.update(config.get("headers") or {})
            site._progress_callback = progress
            emit("message", message="播放地址解析完成，开始下载；分片结束后将合并并校验视频")
            if not site.start_download():
                raise RuntimeError("UAV 下载已取消")
            break
        else:
            raise RuntimeError("UAV 页面解析失败：" + " | ".join(errors))

    files = list(out.glob("*.mp4"))
    if len(files) != 1 or files[0].is_symlink() or files[0].stat().st_size == 0:
        raise RuntimeError("UAV 未生成唯一的非空视频成品")
    emit("message", message="分片合并完成，正在校验视频")
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_type:format=duration", "-of", "json", str(files[0])], capture_output=True, text=True, timeout=30)
    media = json.loads(probe.stdout) if probe.returncode == 0 else {}
    if not any(s.get("codec_type") == "video" for s in media.get("streams", [])) or float(media.get("format", {}).get("duration", 0)) <= 0:
        raise RuntimeError("UAV 成品未通过视频校验")
    target = out / config["filename"]
    if target != files[0]:
        files[0].rename(target)
    emit("progress", percent=100, speed=0, eta=0)
    emit("message", message="视频校验通过，交回 P20 继续标准交付流程")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"UAV 失败：{exc}", file=sys.stderr, flush=True)
        raise SystemExit(1)
