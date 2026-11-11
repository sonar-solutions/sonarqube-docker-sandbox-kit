# syntax=docker/dockerfile:1
# Content recipe for the sonar-claude v3 workload. Found by filename stem, so
# the descriptor needs no `dockerfile:` field.

# MIGRATION NOTE: the v2 kit had no Dockerfile — `extends: claude` pulled in the
# claude agent kit's image and install. v3 workloads bake their own root
# filesystem, so this builds FROM an image that already contains the `claude`
# binary and the hardened `agent` user (uid 1000, home /home/agent). There is no
# literal image to carry over verbatim; confirm BASE_IMAGE points at the
# published claude v3 kit image (or the base the examples/claude workload uses)
# before publishing. The entrypoint execs `claude`, so the binary must be on
# PATH in this base.
ARG BASE_IMAGE=docker/sandbox-templates:claude
FROM ${BASE_IMAGE}

# The kit's runtime files. Kept under files/home/sonar-kit/ so the host-side
# unit tests can keep importing install.py and entrypoint.py from that path.
USER root
COPY files/home/sonar-kit/ /home/agent/sonar-kit/

# MIGRATION NOTE: v2 ran install.py as a create-time `setup.install` hook
# (user "0") with a network grant for binaries.sonarsource.com. It is a
# checksum-pinned artifact download that needs nothing from create time, so it
# moves here: the CLI is baked into the image, digest-pinned and scannable, and
# the install-phase network grant is gone. install.py reads versions.json beside
# it, selects the arch via platform.machine() (resolved per-platform by a
# multi-platform build), verifies the sha256 and installs /usr/local/bin/sonar.
RUN python3 /home/agent/sonar-kit/install.py \
 && chown -R 1000:1000 /home/agent/sonar-kit

# MIGRATION NOTE: v2 `sandbox.entrypoint` and the absence of
# `environment.variables` that could be baked move into the image config here.
# The per-run args reach the container as env vars (declared `env:` in the
# descriptor); the entrypoint derives SONARQUBE_CLI_SERVER from the domain. No
# CMD: v2 `command.default` was empty, and the entrypoint forwards no arguments.
USER agent
WORKDIR /home/agent
ENTRYPOINT ["python3", "/home/agent/sonar-kit/entrypoint.py"]
