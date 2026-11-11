# P22 vivo 固定签名 GitHub Actions 迁移（2026-11-11）

## 决策与边界

- **P22 Project 源码及服务器部署资料继续保存在私有** `tevriqorg/P22-Personal-Relay`；`public-ci` 只持有公开安全的 CI 脚本、配置和核验结果。
- 公共 Actions Runner 不需要发布 P22 Git 历史。构建 job 用 **仅对 P22 仓库 Contents:read** 的短期/可撤销 fine-grained token，按 40 字符不可变 SHA 检出私有源代码。`persist-credentials: false`，不在制品中附带源代码。**Actions 的 public logs 仍然是公开的**，必须只执行无私密数据的 synthetic/JVM 测试，避免构建脚本打印私有信息。
- `fixed_sign=false` 只生成 **UNSIGNED，不可安装** 的中间 APK，绝不用随机 GitHub debug 密钥冒充可升级候选。
- `fixed_sign=true` 单独在 `p22-vivo-fixed-signing` 受保护 Environment 中运行；沿用 **现有** PKCS#12 密钥，不新建签名身份；证书严格钉死 `30ba94601c871ba5c776febbcb3acf35b20259f3272493dd73ac5cb07741c4c2`；校验包名 `tech.bogomolov.incomingsmsgateway` / versionCode >=18 / 非 debuggable。签名不等于 vivo E2E 通过。
- 不触碰 Huawei，不更改东京生产/手机 P22 安装或现有 vivo 配对；签名秘钥绝不进 Git、PR、Issue、log 或 source artifact。
- **不能直接把整个 P22 私有仓库改成 Public**。公开前必须按 `tevriqorg/context/governance/OPEN_SOURCE_POLICY.md` 完整审计 Git 历史、私有 IP/域名/路径、个人数据、提交身份和许可证。

## 一次性配置（必须由有权限的 Owner 安全执行）

1. 在 `tevriqorg/public-ci` 的 Actions secrets 新建 `P22_SOURCE_READ_TOKEN`，仅授权私有 `tevriqorg/P22-Personal-Relay` 的 **Contents:read**。对来源分支使用 GitHub SHA，不用 PR head/不可信 ref。必要时设置 token 过期/轮换。
2. 在 GitHub `Settings → Environments` 建立 `p22-vivo-fixed-signing`，建议设置 Required reviewers 与仅允许 `main` 分支；创建两个 **Environment secrets**：
   - `P22_VIVO_KEYSTORE_P12_B64`（东京受限 PKCS#12 文件编码后的内容）
   - `P22_VIVO_KEYSTORE_PASSWORD_B64`（对应密码文件编码后的内容）
3. 旧材料依然在东京私有位置，**此仓库只记录逻辑引用，不复制完整路径或内容**。迁移签名凭据是一次独立的高信任操作；不要将其内容通过 ChatGPT、PR、GitHub issue 或 shell log 粘贴展示。不能安全迁移时，停止签名并保留现有旧 APK；不能自行生成新 keystore 代替。
4. 环境未配置时仅运行 `fixed_sign=false` 的无签名构建。无论何种情况都不得绕过证书指纹检查或使用 debug/Preview 产物覆盖原 vivo。
5. 在 `Actions → P22 vivo Android fixed-certificate upgrade candidate → Run workflow` 提交当前已审核的 40 位 `source_sha`。若需要安装 APK，再勾选 `fixed_sign` 并由保护环境负责人批准。APK 制品只含程序文件及校验信息，不含配置 HMAC/HPKE 私钥。
6. **正式交付前** 用 `apksigner verify --print-certs` 核对证书、包名和 versionCode，并在 vivo 上验证 `adb install -r`（原包名、相同证书不清除 APP 数据；不要自动执行手机安装）。只有 vivo 真机 SMS/通知加密到 Tokyo/iPhone 通过，才标记对应功能 PASS。

## 注意

- GitHub Environments Secrets 比 Git 历史安全，但把固定私钥交给第三方 GitHub-hosted runner 仍有供应链/工作流变更风险。若不接受这个风险，继续只在东京受控主机进行签名；**不能自动假定用户同意上传私钥**。
- public-ci 为公开仓库，公开 Actions 日志及可下载 artifact。即使 APK 无密钥，其编译代码/错误堆栈/嵌入的 endpoint 仍可能泄露一些非密钥情报。
- 尚未配置 token 与签名 Environment，当前只能审阅工作流；不能宣称已生成同签名安装包。
- P22 源码 Issue 主线：`tevriqorg/P22-Personal-Relay#5`。

:By ChatGPT
