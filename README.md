# Shared public CI runner

This repository hosts manual GitHub Actions workflows for projects whose own
private repositories cannot use Actions quota. Project source remains in its
private repository and is checked out only on an ephemeral GitHub-hosted runner.

## Current profile: P16 Home Vision

1. Create a **fine-grained personal access token** restricted to
   `tevriqorg/P16-Home-Vision`, with repository contents read-only access.
2. Save it as this repository's Actions secret `P16_SOURCE_TOKEN`.
3. Run **Actions → Private project CI → Run workflow** from `main`, select
   `p16-home-vision`, and provide the branch, tag, or commit to test.

The workflow is `workflow_dispatch` only, requires `main`, and has a fixed
project allowlist. It does not accept arbitrary commands, upload source, or
publish build artifacts. Keep each token scoped to one source repository and read-only contents access;
use separate secrets per project. Add new profiles only after reviewing the
checkout and test commands in a pull request.

Workflow logs and ephemeral runner disks are handled by GitHub Actions. Avoid
printing secrets or private source contents in test output.
