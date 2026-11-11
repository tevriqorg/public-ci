# P22 vivo 同签名升级｜公开 CI → 东京固定签名 → P20

## 唯一交付链路

1. 源码仍在私有 `tevriqorg/P22-Personal-Relay`。本公开仓库的 Actions 使用仅对 P22 具有 `Contents:read` 的 `P22_SOURCE_READ_TOKEN`，按**精确 40 位 commit SHA** 从私有源代码编译、单测和产生 **UNSIGNED release APK**。
2. public-ci 必须校验原 vivo `applicationId=tech.bogomolov.incomingsmsgateway`、`versionCode >= 18`、非 debuggable，并存 SHA256/来源记录。它在构建阶段仅生成未签名候选；只有操作者选择 `fixed_sign=true`、并通过 `p22-vivo-fixed-signing` Environment 审批后，才允许使用 GitHub Environment Secrets 恢复**既有**签名密钥进行同证书签名。签名文件不进入 Git 提交。
3. 固定签名运行在 public-ci 受保护 Environment 中，使用 `P22_VIVO_KEYSTORE_P12_B64` 和 `P22_VIVO_KEYSTORE_PASSWORD_B64` 两项现有原签名身份的 Base64 秘密。调用本仓库的 `scripts/p22_vivo_fixed_sign.py`，强制核验证书 SHA256 `30ba94601c871ba5c776febbcb3acf35b20259f3272493dd73ac5cb07741c4c2`、原包名、版本及 APK 摘要。**没有密钥时 fail closed，不创建新签名。**签名结果作为短保留期（1 天）的 Actions 中间 artifact，供 P20 消费。
4. **最终对用户交付仍通过 P20**：东京仅下载并再次核验 Actions 中已签名 APK，按 P20 既有 QQ 闪传 `delivery_backend=qq_flash`、`delivery_profile=unified`、到期 48 小时、需确认流程发布。**尚未接通 P20 自动化**；不得将 Action artifacts 或 GitHub Release 当作用户下载入口，也不得发布 unsigned/debug APK。
5. Vivo 用户在已配对的原应用上**原包名同证书覆盖升级**，保留原配置和设备级 Sender HMAC；**不卸载、不重置配对**。旧 Huawei/P1、Tokyo relay 正式服务不参与构建链路。

## 公开与隐私

- `tevriqorg/public-ci` 是公开仓库，Actions 日志和制品可公开，**签名后的 APK 将短暂出现在公开仓库 Actions artifacts，可能被其他人下载**。不要把真实消息、证书、token、身份信息写入 APK 或 Action artifact。私有源代码在 runner 临时工作目录编译，不主动上传源代码或 runtime 数据；不要在构建时打印原始配置、真实短信、token、Android 私密 HMAC/HPKE。
- 不能把 `tevriqorg/P22-Personal-Relay` 整个仓库简单切成 Public：其中还包含私有服务器架构、路径与历史；需要先依据 `tevriqorg/context/governance/OPEN_SOURCE_POLICY.md` 作完整历史审计。
- Android 源码来自上游并保留 LICENSE。不要把 CI 制品误作实机 PASS：vivo 普通短信、APP 通知、后台恢复和 iPhone 解密通知还需 Human 真机核验。

## 配置门禁

- 需要由拥有授权的账号在 **public-ci 的受保护 Environment `p22-vivo-fixed-signing` Secrets** 中设置 `P22_SOURCE_READ_TOKEN`，仅 P22 源码 `Contents:read`，最小期限/定期轮换。**还必须在受保护 Environment `p22-vivo-fixed-signing` 中配置 `P22_VIVO_KEYSTORE_P12_B64`、`P22_VIVO_KEYSTORE_PASSWORD_B64`。这两项已经由 Human 手工配置且已验证存在。**
- GitHub Actions 运行前必须在主分支启用工作流；手动指定已审查源码 SHA。不得用 PR head 或不受信任 ref。
- 东京原签名材料仍保留备份于 `/home/ubuntu/.config/pxt9/signing/` 下受限文件。Actions Environment Secrets 已复制同一身份；只能由指定审批人批准签名 job，允许分支 `main`；不能把 Base64 当作脱敏形式发送到 Git、Actions 日志、Issue、聊天。
- 下载者必须确认 APK 来自本项目 P22 固定证书，安装前检验 SHA256；P20 仅是传输通道，不替代完整性校验。
- 待接通 Tokyo / P20 交付脚本（此阶段仍未完成），必须从已验证的 `fixed-sign` artifact 下载，不得自动发布来自 PR 或任意外部 URL 的 APK。
- 发布前的人工门禁：签名前版本大于当前 vivo；从 P20 下载的 APK sha256 与签名 manifest 相同；用户执行同包名覆盖升级，未经许可不得执行 `pm clear` 或卸载。
- 任务归属：`tevriqorg/P22-Personal-Relay#5`；CI 项目库：`tevriqorg/public-ci`。

:By ChatGPT
