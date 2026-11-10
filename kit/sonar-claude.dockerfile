# syntax=docker/dockerfile:1
# Content recipe for the sonar-claude v3 workload.
#
# The base is Docker's official v3 claude example image, pinned by digest. It
# carries the platform floor (bash, agent uid 1000, git, CA store) and Claude
# Code. The digest is the multi-platform index of the upstream
# `dhi.io/sbx-templates:claude-code-docker` tag. To update the pin, resolve the
# new digest with
# `docker buildx imagetools inspect dhi.io/sbx-templates:claude-code-docker`,
# change it here and in kit-mixin/sonar-claude-mixin.dockerfile together, and
# rerun the tests, which compare the two. Pulling it requires `docker login dhi.io`.
FROM dhi.io/sbx-templates@sha256:a7a75a11d5b202525fcc70cee2028fbfaf3948c6f448e9f127de84fdf7ae12eb

USER root

# The gate, installer and settings helper are root-owned and read-only to the
# agent, so a session cannot rewrite the checks that gate its next start.
# kit-mixin/sonar-kit/ carries copies of install.py and versions.json (a kit's
# build context cannot reach a sibling directory). Change both copies together;
# the host tests compare them.
COPY --chown=0:0 sonar-kit/ /opt/sonar-kit/

# One root build step, in order:
# 1. The kit runs Python from the base image. Install it only if the base
#    lacks it, and fail the build rather than the first sandbox start.
# 2. The checksum-pinned Sonar CLI is installed in a layer: verified once at
#    build, scannable, and no install-phase egress. The gate still rechecks the
#    version at each start. This covers the CLI only: `sonar integrate claude`,
#    run by the gate on every start, still downloads its components from
#    binaries.sonarsource.com, which the descriptor grants in the runtime phase.
#    `workload` names /usr/local/bin/sonar; the installer accepts no paths.
# 3. WORKDIR is only the image default, kept below the home directory as the
#    official example does so it can never hide ~/.claude. The runtime decides
#    where the workspace is mounted, and the gate uses WORKSPACE_DIR, never this
#    path. The readiness report is written to ~/sonar-kit.
RUN if ! command -v python3 >/dev/null 2>&1; then \
      apt-get update \
      && apt-get install -y --no-install-recommends python3 \
      && rm -rf /var/lib/apt/lists/*; \
    fi \
 && python3 --version \
 && python3 /opt/sonar-kit/install.py workload \
 && mkdir -p /home/agent/workspace /home/agent/sonar-kit \
 && chown agent:agent /home/agent/workspace /home/agent/sonar-kit

USER agent
WORKDIR /home/agent/workspace

# A synchronous, blocking gate that execs Claude only after readiness passes.
# It refuses incoming arguments and always execs bare `claude`, so no option
# can override the verified permission mode. No permission-bypass flags.
# Setting ENTRYPOINT clears the base image's CMD.
ENTRYPOINT ["python3", "/opt/sonar-kit/entrypoint.py"]
