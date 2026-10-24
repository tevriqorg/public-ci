# QQ Flash adapter

这是 P20 Tokyo Transfer Hub 当前使用的 QQ Flash 适配层。

## 作用

server.py 在 Transfer Hub 与 LLBot 之间提供：

- POST /v1/upload?wait=0：异步提交 Flash 上传；
- GET /v1/status?file_set_id=...：查询上传进度；
- POST /v1/events/onebot：接收 LLBot flash_file 事件；
- POST /v1/cancel：按 file_set_id 调用 LLBot cancel_flash_file，真正停止 QQNT Flash 上传并清理 spool；
- uploaded 后安全删除 spool 源文件；
- 上传状态持久化到 spool 下的 .p20-flash-state.json；
- GET /health：显示 pending 与 spool 文件数。

LLBot 的 HTTP Post 事件使用 OneBot Token 做 HMAC-SHA1 签名。适配器验证 X-Signature: sha1=... 后才接收事件。

## LLBot 配置

在现有 ob11.connect 中保留 HTTP API，并新增 llbot-http-post.example.json 对应的 http-post 条目。

不要把实际 OneBot Token 写进 Git。

## 环境变量

- QQXFER_BIND，默认 Tokyo Tailscale IP；
- QQXFER_PORT，默认 17600；
- QQXFER_BASE；
- QQXFER_SPOOL；
- QQXFER_API_TOKEN_FILE；
- QQXFER_OB_TOKEN_FILE；
- QQXFER_OB_BASE；
- QQXFER_FLASH_WAIT_TIMEOUT。

## LLBot 7.12.15 P20 patch

上游稳定基线仍是 LLBot 7.12.15，不修改 upstream/runtime.lock.yaml 中的上游版本语义。Tokyo 生产额外使用一个可复现 P20 patch：

- 固定上游 commit：e0d5d2067651b0e526c841addaa3b7de51d2dd8c；
- patch：llbot-7.12.15-flash-cancel.patch；
- 构建定义：Dockerfile.llbot-7.12.15-flash-cancel；
- Tokyo 生产镜像：p20-llbot:7.12.15-p20.1。

这个 patch 只增加 cancel_flash_file → QQNT stopFileSetUpload(fileSetId)，并把 OneBot Token 日志改为 <redacted>。不修改既有 Flash 上传协议流程。

## 已验证

Tokyo 实测：

- 新文件 submitted → uploading → uploaded；
- 128 MiB 非秒传上传持续产生 uploaded_size / total_size / speed / remain_seconds；
- uploaded 后 spool 自动删除；
- Transfer Hub 可轮询状态并写入 job.upload_progress SSE/SQLite 事件。
- 128 MiB 全新随机文件在上传到 8 MiB 时直接调用 cancel_flash_file，之后 8 秒 uploaded_size 增长为 0，未出现 uploaded。
- 64 MiB 文件经完整 Transfer Hub 链路上传到 10.9% 后 Cancel，最终 Job=CANCELLED，qqxfer pending=0 / spool=0，Transfer Hub 工作副本与上传临时文件均清理。

:By ChatGPT
