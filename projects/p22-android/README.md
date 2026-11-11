# P22 Android CI snapshot

This folder contains only a buildable, reviewed source snapshot of the Android application from private `tevriqorg/P22-Personal-Relay`. The private project is canonical. No private relays, keys, passwords, server code, runtime logs or configs are copied here.

`SOURCE_REVISION` pins the exact source commit. The test-app relay DNS literal in Java/test files is replaced with `relay.p22.example.invalid` for public source review. CI restores the expected existing public HTTPS enrollment endpoint from an environment secret at build time; never store that runtime endpoint in this public source tree.

The upstream Android application retains `android-forwarder/LICENSE.txt` (MIT). Any snapshot change needs review and security screening; do not auto-sync unreviewed private source changes. The final APK must preserve the existing package name and vivo signing identity and is delivered using P20 after verification.

:By ChatGPT
