# Project instructions

Sonar integration for Docker Sandboxes: two Docker Sandbox Kit v3 kits, a
blocking workload (`kit/`) and an install-only mixin (`kit-mixin/`). Read
`README.md` and `CONTRIBUTING.md` first.

## Durable decisions

- The official Docker Sandbox Kit v3 specification is the authority for
  descriptors and recipes. Older schema versions are not.
- The workload's root-owned entrypoint is a blocking gate that execs exactly
  bare `claude` and refuses any forwarded argument. Never add a launch path that
  bypasses the gate or enables permission-bypass modes.
- A mixin cannot own the entrypoint and inherits its base workload's runtime.
  Keep that distinction explicit in descriptors and docs.
- Sonar component downloads are allowed only in the phase where integration
  runs: runtime for the workload, install for the mixin.
- Region targets are exact: `cloudDomain` and `cloudServer` name the same
  region in both descriptors and in the gate.
- `install.py` and `versions.json` are kept identical in `kit/sonar-kit/` and
  `kit-mixin/sonar-kit/`. Both recipes pin the same base image digest.
- Use native Docker CLI commands (`docker buildx`, `sbx kit`); this repository
  has no wrapper launcher.
- CI stays offline: unit tests only, no image pulls and no registry credentials.
- Use `uv` for Python (`uv sync --locked`, `uv run --locked ...`).
- Keep committed files public-safe and person-neutral: no personal paths,
  accounts, progress notes or private identifiers. Working state belongs in an
  ignored `.local/STATUS.md`.
- Planning context lives in the project's Project Hub initiative, outside this
  repository.
- Repository-visible source, docs, comments, branches, commits and PRs must not
  include implementation-tool authorship or workflow attribution. Required
  product names remain.
