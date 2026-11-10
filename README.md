# Sonar integration for Docker Sandboxes

Run Claude Code in a Docker Sandbox with Sonar guidance and agentic analysis
configured before the agent starts. The kit installs a checksum-pinned SonarQube
CLI and checks authentication, project integration and agent hooks at startup.
It supports SonarQube Cloud EU and US.

This is a reference integration, provided as is and without warranty. It is not
affiliated with or endorsed by Docker, Inc. or Anthropic. Docker, Docker
Sandboxes and Claude Code are trademarks of their respective owners.

## Kits

The repository holds two Docker Sandbox Kit v3 kits, built as OCI images. The
normative reference is the official
[Docker Sandbox Kit v3 specification](https://github.com/docker/sandbox-kit-spec/blob/main/docs/spec/SPEC-v3.md).

| Kit | Directory | Use it for |
| --- | --- | --- |
| Workload `sonar-claude` | `kit/` | The supported path. A root-owned, blocking entrypoint gates Claude on Sonar readiness at every start. |
| Mixin `sonar-claude-mixin` | `kit-mixin/` | Layering the CLI and a create-time integration onto another workload that provides `claude`. |

The workload gate verifies the pinned CLI, authentication, project integration,
required features and Claude hook references, then starts exactly `claude` with
no forwarded arguments. A mixin cannot own the entrypoint, so it inherits the
base workload's launch and runtime: integration runs once at create, there is no
blocking readiness gate, and Docker's official Claude workload launches with
permission prompts bypassed, which a mixin cannot change. Use the workload when
the gate or prompt-preserving permission mode matters. This repository provides
no way to launch the agent with the gate bypassed.

## Prerequisites

- Docker Sandboxes `sbx` 0.51.0 or later (kit schema v3).
- Docker with Buildx, and a Buildx builder that uses the `docker-container`
  driver (see below).
- Access to the base image `dhi.io/sbx-templates:claude-code-docker`, which
  requires `docker login dhi.io`. The recipes pin it by digest.
- A platform supported by [Docker Sandboxes](https://docs.docker.com/ai/sandboxes/install/).
- An existing SonarQube Cloud project with Vortex enabled and a Sonar user token.
- A source workspace for the agent to work in.

## Configure credentials

Sign in and store the Sonar user token through Docker's hidden prompt:

```sh
sbx login
sbx secret set sonar-cloud
```

Log in to the registry that serves the base image:

```sh
docker login dhi.io
```

Configure Claude using its supported sign-in flow, or store an API key with
`sbx secret set anthropic`. Never put credentials in source files, kit arguments
or command-line values. Docker's credential proxy injects the Sonar token into
approved outbound requests; approve the regional credential binding if Docker
prompts for it.

## Build, inspect, run and export

Clone this repository and run every command below from its root. Create a
named `docker-container` builder once. The default `docker` driver cannot build
a kit image. Export `BUILDX_BUILDER` before any `sbx` command: `sbx` builds a
source kit with Buildx itself, including during `sbx kit inspect`, and this
makes it use the named builder.

```sh
docker buildx create --name sonar-kit-builder --driver docker-container
export BUILDX_BUILDER=sonar-kit-builder
```

A kit is built from its **descriptor** (`sonar-claude.yaml`), with the kit
directory as the build context. The v3 build frontend reads the companion recipe
(`sonar-claude.dockerfile`) by filename stem; building the recipe directly
produces an ordinary container image without the kit descriptor. Replace
`YOUR_ORG` and `YOUR_PROJECT` with your SonarQube Cloud keys. Pass **both**
region arguments from one choice: `cloudDomain` and `cloudServer` must name the
same region, and the gate refuses a mismatch.

| Region | `cloudDomain` | `cloudServer` |
| --- | --- | --- |
| EU | `sonarcloud.io` | `https://sonarcloud.io` |
| US | `sonarqube.us` | `https://sonarqube.us` |

```sh
docker buildx build --builder sonar-kit-builder \
  --file kit/sonar-claude.yaml --output type=cacheonly kit

sbx kit inspect ./kit \
  --kit-arg organization=YOUR_ORG \
  --kit-arg project=YOUR_PROJECT \
  --kit-arg cloudDomain=sonarcloud.io \
  --kit-arg cloudServer=https://sonarcloud.io
```

Source kit paths must be explicit relative or absolute paths such as `./kit`; a
bare word is read as an agent or sandbox name. Run the workload against a
workspace (replace `../your-project`) under a stable sandbox name:

```sh
sbx run --name sonar-claude-project ./kit ../your-project \
  --kit-arg organization=YOUR_ORG \
  --kit-arg project=YOUR_PROJECT \
  --kit-arg cloudDomain=sonarcloud.io \
  --kit-arg cloudServer=https://sonarcloud.io
```

To export a kit as a local OCI layout instead of running it (the destination
must not exist), for either directory:

```sh
docker buildx build --builder sonar-kit-builder \
  --file kit/sonar-claude.yaml \
  --output type=oci,dest=dist/sonar-claude-oci,tar=false kit
```

Build the mixin the same way with `--file kit-mixin/sonar-claude-mixin.yaml`
and the `kit-mixin` context, and run `sbx kit inspect ./kit-mixin` with the same
arguments. Registry publication and signing use Docker's native tools and are
out of scope here.

## Workspace and readiness report

Docker assigns the workspace through `WORKSPACE_DIR`; the gate never guesses
one and does not start Claude if it is unset, relative or missing. Within the
sandbox the gate writes an owner-only report to `~/sonar-kit/readiness.json`
before it starts Claude, and removes any previous report first so a stale
success never survives a failed start. A `configured` result means setup passed;
it does not mean a live analysis succeeded.

Inside the agent workspace, use Sonar's generated instructions and hooks. To
request a file analysis directly, replace `path/to/file.py` and `YOUR_PROJECT`:

```sh
sonar analyze agentic --depth DEEP --file path/to/file.py --project YOUR_PROJECT
```

Keep any analysis errors, skipped-file reports or context warnings. A recent
conventional CI analysis supplies richer project context; zero findings on one
file are not a full-project security assessment.

Stop the sandbox with `sbx stop sonar-claude-project`. To resume, rerun
`sbx run --name sonar-claude-project` with the same kit path, workspace and kit
arguments, and with `BUILDX_BUILDER` still exported.

## Troubleshooting

- **Build cannot pull the base image:** run `docker login dhi.io` and confirm
  your account can access it.
- **Builder error:** use a named builder with the `docker-container` driver and
  `BUILDX_BUILDER` set for `sbx`.
- **Credentials missing:** store `sonar-cloud` and approve its regional binding.
- **Startup gate fails:** verify the selected region, project permissions and
  Vortex entitlement. Do not disable hooks or isolation to bypass the check.
- **Version or checksum mismatch:** review the pins in
  `kit/sonar-kit/versions.json`; do not bypass checksum verification.
- **Connection or daemon failure:** inspect `sbx policy log` and `sbx diagnose`.

The kit depends on the pinned CLI's integration-state schema. Revalidate the
startup checks when upgrading the CLI or Docker runtime.

## Develop

Host tests use Python 3.12 or 3.13 and `uv`; no third-party Python dependencies
are required. The sandbox runtime uses its image's `python3`. The tests are
offline and need neither Docker nor registry access.

```sh
uv sync --locked
uv run --locked python -m unittest discover -s tests -v
```

## Contributing, security and license

See [CONTRIBUTING.md](CONTRIBUTING.md). Report vulnerabilities as described in
[SECURITY.md](SECURITY.md), not in public issues. This project is licensed under
the [GNU Lesser General Public License v3.0 or later](LICENSE), which supplements
the [GNU General Public License v3.0](COPYING).

## References

- [Docker Sandbox Kit v3 specification](https://github.com/docker/sandbox-kit-spec/blob/main/docs/spec/SPEC-v3.md)
- [Docker Sandboxes documentation](https://docs.docker.com/ai/sandboxes/)
- [Docker credential bindings](https://docs.docker.com/ai/sandboxes/configuration/credentials/)
- [Sonar Claude Code integration](https://docs.sonarsource.com/sonarqube-cli/integrations/claude-code)
