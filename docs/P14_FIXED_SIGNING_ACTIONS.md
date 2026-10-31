# P14 Fixed-Signing Test APK in GitHub Actions

## One-time authorized setup

The owner must add three environment secrets under tevriqorg/public-ci >
Settings > Environments > p14-test-signing > Environment secrets.

The TEST certificate must retain the pinned SHA256 fingerprint:
dfa33df904e4eb3ca30fdc61a1817dbec36d131a94a594663f7d79522430cc94

Existing signing source is held on the old authorized signing machine:
 /home/ubuntu/.local/share/rikkahub-p14-signing/recovery-20261020/

Execute once on that machine in an authenticated gh CLI terminal (NO signing occurs there):

    set -euo pipefail
    dir=/home/ubuntu/.local/share/rikkahub-p14-signing/recovery-20261020
    base64 -w0 "$dir/p14-fixed.p12" | gh secret set P14_TEST_KEYSTORE_P12_B64 -R tevriqorg/public-ci --env p14-test-signing
    base64 -w0 "$dir/keystore.password" | gh secret set P14_TEST_KEYSTORE_PASSWORD_B64 -R tevriqorg/public-ci --env p14-test-signing
    base64 -w0 "$dir/certificate.der" | gh secret set P14_TEST_SIGNER_CERT_DER_B64 -R tevriqorg/public-ci --env p14-test-signing
    gh secret list -R tevriqorg/public-ci --env p14-test-signing

Do not store the key or password in Git, PR descriptions, action logs or chat.
This once-only transfer does not compile or sign APK files on the VPS.

## Every future candidate

Visit tevriqorg/public-ci > Actions > P14 ARM64 candidate build + fixed signing >
Run workflow (main branch). Supply the exact Android source commit SHA.

- Job 1 compiles and tests Android sources with NO signing secrets.
- Job 2 downloads the same-run APK artifact, checks its provenance, reads the
  restricted environment secrets, checks pinned certificate + package identity,
  signs using Android SDK apksigner and uploads a FIXED-SIGNED installable
  test APK with checksum and manifest. Key files are never uploaded.
- Set publish_test_release=true only when intentionally delivering a human
  test candidate. A GitHub TEST prerelease is then created in public-ci,
  without any external PAT or any VPS signing/upload steps.
- Signing is never silently skipped; missing secrets fail the job.

## Identity and key lifecycle

This is the old P14 test-only certificate for package me.rerere.rikkahub.debug.
Keep it separate from future formal release keys. App label and icon do not
control the Android installation identity: changing package/applicationId or
changing signing cert without valid rotation prevents an ordinary in-place update.
Keep debug versionCode monotonic and perform device/data migration tests.
