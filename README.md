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

P22 私有项目的 Android unsigned APK 可在公共 runner 编译；**仅东京本地固定证书签名后通过 P20 QQ 闪传交付**，不在 GitHub Action/Release 发布可安装 APK，不上传任何生产私钥。门禁与操作说明见 [`docs/P22_VIVO_FIXED_SIGNING.md`](docs/P22_VIVO_FIXED_SIGNING.md)。
