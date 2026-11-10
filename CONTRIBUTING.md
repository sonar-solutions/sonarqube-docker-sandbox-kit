# Contributing

Thanks for your interest in this project. Bug reports, questions and suggestions
are welcome as [GitHub issues](../../issues). Please do not include credentials,
tokens or private project details in an issue.

Pull requests from outside Sonar are not accepted at this time, so that changes
to the pinned CLI version, checksums and credential handling stay under a single
review. The source is public: you are welcome to fork and experiment.

To report a security vulnerability, follow [SECURITY.md](SECURITY.md) instead of
opening an issue.

## Development

Host tests use Python 3.12 or 3.13 and [uv](https://docs.astral.sh/uv/). There
are no third-party dependencies. The tests are offline and run in CI.

```sh
uv sync --locked
uv run --locked python -m unittest discover -s tests -v
```

Building the kit images needs Docker Buildx with a named `docker-container`
builder, `sbx` 0.51.0 or later and `docker login dhi.io` for the base image; see
the README. CI does not pull images or use registry credentials, so build,
inspect, run and export checks are done locally.

Guidelines for changes in this repository:

- Treat the official Docker Sandbox Kit v3 specification as the authority for
  descriptors and recipes.
- Update `kit/sonar-kit/versions.json` (release and both checksums) together,
  and verify the checksums against the published binaries.
- Keep `install.py` and `versions.json` identical in `kit/sonar-kit/` and
  `kit-mixin/sonar-kit/`; a build context cannot reach a sibling directory, and
  a test compares the copies.
- To update the base image, resolve the new digest with
  `docker buildx imagetools inspect dhi.io/sbx-templates:claude-code-docker`
  and change the `FROM` line in both recipes together; a test checks they agree.
- Do not weaken credential redaction, the startup readiness gate or the
  sandbox's network and isolation policy. Never add a way to launch Claude
  with forwarded arguments or with permission prompts bypassed.
- Keep `cloudDomain` and `cloudServer` consistent in both descriptors and the
  gate; tests enforce the exact pairs.
- Keep the kit versions in both descriptors, `pyproject.toml` and `uv.lock` in
  sync; a test enforces this.
