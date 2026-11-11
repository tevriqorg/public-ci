# P22 vivo 同签名升级｜公开 CI → 东京固定签名 → P20

## 唯一交付链路

1. 源码仍在私有 `tevriqorg/P22-Personal-Relay`。本公开仓库的 Actions 使用仅对 P22 具有 `Contents:read` 的 `P22_SOURCE_READ_TOKEN`，按**精确 40 位 commit SHA** 从私有源代码编译、单测和产生 **UNSIGNED release APK**。
2. public-ci 必须校验原 vivo `applicationId=tech.bogomolov.incomingsmsgateway`、`versionCode >= 18`、非 debuggable，并存 SHA256/来源记录。它**不产生安装包，不使用临时签名，不将原证书私钥上传 GitHub。**
3. 由东京 VPS 上的受控 `p22_ci_p20_delivery.py` 消费经过核验的 Actions unsigned artifact，复核来源、版本、文件摘要，再使用**现有、未移动的** PKCS#12 本地签名，调用本仓库公开可审计的 `scripts/p22_vivo_fixed_sign.py`。证书 SHA256 必须是 `30ba94601c871ba5c776febbcb3acf35b20259f3272493dd73ac5cb07741c4c2`，签名后重新进行 `apksigner verify`、包名和 versionCode 核验。
4. **只将最终验证通过的已签名 APK 交给 P20**，通过 P20 现有 QQ 闪传 `delivery_backend=qq_flash` 和 `delivery_profile=unified` 生成到期 48 小时、需收件人确认的传输任务。不得改走 GitHub Release；失败状态不得上传 unsigned/debug APK。
5. Vivo 用户在已配对的原应用上**原包名同证书覆盖升级**，保留原配置和设备级 Sender HMAC；**不卸载、不重置配对**。旧 Huawei/P1、Tokyo relay 正式服务不参与构建链路。

## 公开与隐私

- `tevriqorg/public-ci` 是公开仓库，Actions 日志和制品可公开。私有源代码在 runner 临时工作目录编译，不主动上传源代码或 runtime 数据；不要在构建时打印原始配置、真实短信、token、Android 私密 HMAC/HPKE。
- 不能把 `tevriqorg/P22-Personal-Relay` 整个仓库简单切成 Public：其中还包含私有服务器架构、路径与历史；需要先依据 `tevriqorg/context/governance/OPEN_SOURCE_POLICY.md` 作完整历史审计。
- Android 源码来自上游并保留 LICENSE。不要把 CI 制品误作实机 PASS：vivo 普通短信、APP 通知、后台恢复和 iPhone 解密通知还需 Human 真机核验。

## 配置门禁

- 需要由拥有授权的账号在 **public-ci 的 Actions Repository Secret** 中设置 `P22_SOURCE_READ_TOKEN`，仅 P22 源码 `Contents:read`，最小期限/定期轮换。**无需设置 P22 的 GitHub signing Environment secrets。**
- GitHub Actions 运行前必须在主分支启用工作流；手动指定已审查源码 SHA。不得用 PR head 或不受信任 ref。
- 东京签名材料仍在 `/home/ubuntu/.config/pxt9/signing/` 下 root/owner-only 的受限文件，迁移/读取该材料并不需要在 Git、Actions、Issue、聊天中显示内容。
- 下载者必须确认 APK 来自本项目 P22 固定证书，安装前检验 SHA256；P20 仅是传输通道，不替代完整性校验。
- 向 Tokyo 安装和启用签名/P20 发布脚本必须由受控运行操作完成，检查无效时 fail closed，不把旧文件覆盖成新文件。
- 发布前的人工门禁：签名前版本大于当前 vivo；从 P20 下载的 APK sha256 与签名 manifest 相同；用户执行同包名覆盖升级，未经许可不得执行 `pm clear` 或卸载。
- 任务归属：`tevriqorg/P22-Personal-Relay#5`；CI 项目库：`tevriqorg/public-ci`。

:By ChatGPT
