# Shared public GitHub Actions runner

This repository runs builds and tests for projects whose own GitHub Actions
are unavailable. Project sources used by this runner are stored under
`projects/`; anyone can inspect the code and workflow because this repository
is public.

## P16 Home Vision

The current P16 source snapshot is under `projects/p16-home-vision/implementation/`.
`projects/p16-home-vision/SOURCE_REVISION` records its private-repository source
commit. The copy contains tracked implementation, tests, fixtures, and build
configuration. Runtime secrets, local databases, and household media are not
part of the snapshot.

Run **Actions → Private project source CI → Run workflow** from `main` and
select `p16-home-vision`. The runner builds and tests the checked-in snapshot
on an ephemeral GitHub-hosted runner. It does not fetch another repository,
need a source key, or upload build artifacts.

Project snapshots are updated through reviewed commits. Never add credentials,
private runtime configuration, local state, or real household media here.

## P22 Personal Relay Android

P22 使用仅包含 Android 编译必需文件的 public-safe snapshot：projects/p22-android/。SOURCE_REVISION 指明私有仓库源码版本。运行 P22 Android snapshot fixed-signing candidate (P20 only) 时不需要专用 GitHub Token；人工审批后使用受保护 Environment 的原证书完成固定签名，最终 APK 仍由东京通过 P20 QQ 闪传交付（P20 自动对接待验收）。不允许复制整个私有 P22 仓库。操作与风险见 docs/P22_VIVO_FIXED_SIGNING.md。
