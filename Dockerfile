# syntax=docker/dockerfile:1@sha256:572cebbc2b15e6f84c077742952674889cb63cd5e9b80abdffcbe587b6f19183

# Container Apps runs linux/amd64 only, and the dev host here is arm64 (Docker
# Desktop on Apple Silicon). A native build produces an image that crash-loops
# with an exec format error and no other clue, so every build for deployment
# must pass --platform linux/amd64. The Makefile's `build` target does it rather
# than leaving it to memory.

# The builder is the official Go image at **exactly mise.toml's go**: local
# runs and CI test with that Go, and this is the one that compiles what ships.
# The shared Renovate preset (jay-withers/renovate go.json) moves this tag in
# the same "mise tools" PR as mise.toml's go, so the two cannot drift. Keep the
# full patch version in the tag, or a mise patch would move alone.
FROM golang:1.27.4-trixie@sha256:430985542100b871e108ccc00e8dcc5eef08ca14ee5825e98ebf563d8ffec4ee AS builder

WORKDIR /src

# Modules before source, so editing a package does not re-download them.
COPY go.mod go.sum ./
RUN --mount=type=cache,target=/go/pkg/mod go mod download

COPY cmd ./cmd
COPY internal ./internal
# CGO off: a static binary, which is what lets the runtime image have no libc.
# -trimpath keeps the builder's paths out of the binary; -s -w drops the
# symbol tables nothing reads in production. GOTOOLCHAIN=local, as in CI.
RUN --mount=type=cache,target=/go/pkg/mod --mount=type=cache,target=/root/.cache/go-build \
    CGO_ENABLED=0 GOOS=linux GOARCH=amd64 GOTOOLCHAIN=local \
    go build -trimpath -ldflags="-s -w" -o /out/repoagent ./cmd/repoagent


# Distroless static: CA certificates, tzdata and /etc/passwd, and nothing
# else: no shell, no package manager, nothing for Trivy to find but the
# binary. Unlike the estate's Python apps, this is not built on
# dev-containers' python-app (a deliberate exception, see docs/design.md).
FROM gcr.io/distroless/static-debian13@sha256:4c2cd1dee442c9a99924a378c7c126fac8fef14771579ef567c3f2bdd7a5122a

# This label is what connects the pushed package to this repository, and that
# connection is what gives a workflow's GITHUB_TOKEN write access to it. Without
# it a package pushed by hand is user-scoped and unlinked, and cd-publish fails
# with `denied: permission_denied: write_package` however its `packages: write`
# permission is declared.
LABEL org.opencontainers.image.source="https://github.com/jay-withers/repo-agent"

# On PATH, so the ENTRYPOINT below resolves by name as the Python console
# script did.
COPY --from=builder /out/repoagent /usr/local/bin/repoagent

# Numeric, because a runtime enforcing runAsNonRoot has to resolve the user
# without reading /etc/passwd, and the same uid the Python image ran as, so
# nothing about the job's identity changes. repo-agent's container.runs_as_root
# reads only this file.
USER 10001

# No EXPOSE and no HEALTHCHECK: this image serves nothing. It is a job that runs
# to completion, and a healthcheck on a process with no listener would only ever
# report unhealthy.

# **This ENTRYPOINT is the single source of truth for the executable's name.**
# Terraform deliberately sets no `command` on the job — it sets `args` only — so
# that renaming the binary is one atomic change here rather than a change that
# has to be mirrored in another repository's Terraform. See
# terraform/main.container-apps-jobs.tf for the outage that taught this.
ENTRYPOINT ["repoagent"]
CMD ["scan"]
