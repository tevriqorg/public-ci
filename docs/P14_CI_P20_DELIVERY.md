# P14R3 CI-to-P20 signed ARM64 test delivery

Authority: [tevriqorg/P14-Entry#51](https://github.com/tevriqorg/P14-Entry/issues/51).
This replaces the old RikkaHub `p14-debug-` GitHub Release polling path; it does **not** formally publish an APK or relax acceptance gates.

1. A SUCCESS run of `tevriqorg/public-ci` workflow `p14-arm64-candidate.yml` must upload `p14-arm64-UNSIGNED-<source sha>` containing APK, `SOURCE_SHA`, and `UNSIGNED-SHA256SUMS`.
2. Tokyo reads the run via its already authorized `gh` authentication and verifies the source/digest and package constraints.
3. Tokyo signs with the **existing** P14 PKCS#12 key; only certificate SHA-256 `dfa33df904e4eb3ca30fdc61a1817dbec36d131a94a594663f7d79522430cc94` is accepted. APK Signature Scheme v2/v3 are mandatory. Never upload secrets to the public workflow or auto-create another key.
4. Tokyo POSTs the **verified signed APK** to the existing P20 Hub using its private Tailnet endpoint and creates an encrypted QQ Flash transfer (AES-256, receiver ACK, 48-hour TTL).
5. The resulting job receives P20 watcher metadata. The **existing P20 watchd READY notifier** sends the signed webhook through the already configured private Tokyo Hermes/QQ endpoint only when the transfer becomes READY. This system does not request any LLM run or modify Hermes Core.
6. A durable state file and unique CI-run filename dedupe retries, including errors after upload but before the state checkpoint. No second job is created for the same run.
7. The **existing** systemd user timer `p20-rikkahub-release-watch.timer` is reused; old release polling is disabled by changing only the service ExecStart. The former service body and state remain available for rollback.

Source file: `ops/p14_arm64_ci_delivery.py`. On Tokyo the maintained copy lives under `/home/ubuntu/p20-ops/integrations`; private signed candidates remain under `/home/ubuntu/p20-transfer/p14-remote/ci-signed-arm64` and are not published. Avoid direct browser keys or public APK links. For dry-run use `python3 ops/p14_arm64_ci_delivery.py --dry-run`. First run is gated to CI run 40148099245 or later.

If CI, signing, provenance, Hub upload or QQ tagging fails, the timer must log an error, retain retryable state and must not announce a fake READY. Actual QQ delivery success must be verified by the P20 `job.watcher_notification` event.

Scope: applies to test package `me.rerere.rikkahub.debug` only, no device-data migration, no production APK publishing, no private repo build, no Hermes Agent invocation.
