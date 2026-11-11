# P22 vivo 固定签名｜公开 CI 快照 → P20

## 方案

- 私有 tevriqorg/P22-Personal-Relay 是唯一正式产品源码仓库，整个仓库绝不能直接公开。
- 公开 tevriqorg/public-ci 只存放经过安全审查的 Android 构建源码快照：projects/p22-android/android-forwarder/，由 SOURCE_REVISION 记录原仓库的确切 commit。中继端、服务器、PWA、runtime、数据库和部署文件均不迁移。
- 工作流仅 checkout 本公开仓库，直接构建本地快照，不访问 private P22，也不需要 PAT 或 P22_SOURCE_READ_TOKEN。
- 快照源码内硬编码的生产 HTTPS relay 域名已替换为 relay.p22.example.invalid。环境 p22-vivo-fixed-signing 中的 P22_RELAY_HOST Secret 只在构建执行期间恢复真实主机名，不提交到 Git 历史。但最终 APK 本身仍可能暴露它需要连接的服务域名。
- Human 已设置原 vivo 固定签名身份 P22_VIVO_KEYSTORE_P12_B64 和 P22_VIVO_KEYSTORE_PASSWORD_B64。只在审批通过的 GitHub Actions Environment 的 main 工作流内使用，不生成替代证书。
- 运行路径：GitHub Actions → P22 Android snapshot fixed-signing candidate (P20 only) → Run workflow。fixed_sign=true 默认执行单次编译、JVM 测试、原证书签名和指纹核验。
- 构建校验包名 tech.bogomolov.incomingsmsgateway、非 debuggable、versionCode 不低于18、来源 SHA 和文件 SHA256。旧证书指纹必须为 30ba94601c871ba5c776febbcb3acf35b20259f3272493dd73ac5cb07741c4c2。失败时不得退回新证书。
- 生成的 Actions APK 是交给 P20 的临时中间制品，不是 GitHub Release 或用户交付入口。最终应由东京复核 APK 签名、版本、SHA256 后经 P20 QQ 闪传（qq_flash, unified, 48h, ack required）发布。**P20 对接尚待实施验证。**

## 安全与版本限制

- public-ci 是公开仓库，源码快照与 Actions Artifact 可被外部查看；不要加入任何真实短信、HMAC、HPKE 私钥、Token、证书原文、真实日志、内部网络/用户身份。
- 已对 Java/Gradle/XML、资源和测试的常见凭据、专用 IP/主机名、绝对路径及身份信息做初步检查；安全审查不是绝对无风险保证。
- GitHub-hosted runner 可以访问受保护的 Secret；审批和 main 分支限制不能替代对公开工作流的代码审查。已签名 APK artifact 在公开仓库保留最长 1 天，期间有可能被他人下载。
- 快照必须人工更新并逐次复审；禁止自动复制整个私有 Project、实际数据或未审查内容。
- vivo 必须使用原包名与同一个证书覆盖升级（versionCode 不低于当前设备版本）；不能卸载或重置已经配对的 sender。Huawei/P1 USB 和 Tokyo relay 正式运行环境均不变。完整 SMS/通知 E2E 及 P20 发布待实机验收。
- 上游软件 MIT License 保留在 Android 快照内。

跟踪：tevriqorg/P22-Personal-Relay#5。

:By ChatGPT
