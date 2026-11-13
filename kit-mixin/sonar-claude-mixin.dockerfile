# syntax=docker/dockerfile:1
# Overlay for the sonar-claude mixin: only the checksum-verified Sonar CLI.
# No ENTRYPOINT, CMD or WORKDIR — a mixin's contract fields are ignored at
# assembly, and the base workload's launch command stays in force.
#
# The build stage reuses the workload's base, pinned by the same digest, so the
# same installer runs the same way. To update the pin, see
# ../kit/sonar-claude.dockerfile and change both together. sonar-kit/install.py
# and versions.json are copies of ../kit/sonar-kit/ (a build context cannot
# reach a sibling directory); change both together. The host tests compare them.
FROM dhi.io/sbx-templates@sha256:a7a75a11d5b202525fcc70cee2028fbfaf3948c6f448e9f127de84fdf7ae12eb AS build
USER root
COPY --chown=0:0 sonar-kit/ /tmp/sonar-kit/
RUN if ! command -v python3 >/dev/null 2>&1; then \
      apt-get update \
      && apt-get install -y --no-install-recommends python3 \
      && rm -rf /var/lib/apt/lists/*; \
    fi \
 && python3 /tmp/sonar-kit/install.py overlay \
 && chown -R 0:0 /out \
 && chmod 0755 /out/usr /out/usr/local /out/usr/local/bin

# The overlay ships nothing under /home, so the base keeps root's /home and
# uid 1000's /home/agent. Every shipped entry is 0/0.
#
# The final stage is file content only: it holds no shell or entrypoint, so it
# cannot run as a container on its own, and running it standalone is not
# supported. It still declares an unprivileged numeric USER (1000, the
# platform's `agent` uid) so the image config does not default to root. This is
# image metadata only: v3 records a mixin's USER but ignores it at assembly, so the composed sandbox
# keeps running as the base workload's declared user and no file ownership
# changes. Every shipped entry stays 0/0.
FROM scratch
COPY --from=build /out /
USER 1000
