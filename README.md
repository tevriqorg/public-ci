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
