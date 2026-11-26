# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo does

One Python package (`repoagent`), one image, one Container Apps job. It reads
every GitHub repository the App is installed on, checks Renovate is actually
working, and emails a weekly digest that is waiting on Monday morning.

`README.md` covers *why* and how to run it. This file is the rules, one line
each. The reasoning behind them, and the incidents that made them, are in
[docs/design.md](docs/design.md) and [docs/checks.md](docs/checks.md): read the
linked section before changing the thing a rule protects.

**A Go port is in progress beside it** (`cmd/`, `internal/`; see "The Go
port" below). The Python still ships.

**Status: working.** It authenticates, lists every repository, fetches detail
for each with one batched GraphQL query, runs deterministic checks, asks
DeepSeek to triage the results, and emails a digest.

## Commands

```bash
make install   # pre-commit hooks + Python dependencies
make test      # the suite: no network, no Azure, no credentials
make lint      # every pre-commit hook
make run       # repoagent render: scans real GitHub, prints the digest, sends nothing
make validate  # terraform init -backend=false + validate
make deploy IMAGE_TAG=vX.Y.Z
make state     # print what the scan remembers between runs
make go-test   # the Go port: gofmt, vet, golangci-lint, go test -race
make golden    # regenerate testdata/golden from the Python scan
```

`make` with no target lists the rest.

## Rules

### State and suppressions ([history](docs/design.md#history))

- **`Finding.id` is load-bearing**: a hash of `repo:check`. Change how it's
  computed and every finding in the estate reads as new for a week.
- **State is saved last, after the email, and only if it was sent.** `render`
  never saves.
- `repoagent suppress` requires `--reason`. An unparseable expiry *expires*.
  The operator commands report failure; the scan never does.

### The Go port ([Go port](docs/design.md#the-go-port))

- `cmd/` and `internal/` are a Go port **beside** the Python, which still
  ships. It must change nothing visible: digest, `Finding.id`, state JSON,
  prompt, query, CLI.
- **Parity is the golden test.** A change to the Python's output changes the
  Go in the same PR: edit the fixture if needed, `make golden`, then
  `make go-test`. Never hand-edit a golden file.
- Python formatting goes through `internal/pyfmt` (`repr`, `{:,}`,
  `splitlines`, `html.escape`, `json.dumps`), never Go's look-alikes.
- Logs keep Python's line format, not JSON: obsview groups errors by
  masking quoted values.

### Infrastructure ([two-repo split](docs/design.md#the-two-repo-split), [`command`](docs/design.md#the-command-decision--do-not-undo-this), [deploy](docs/design.md#make-deploy-is-az-cli-not-terraform-apply))

- Platform resources are resolved **by name through data sources**, never
  `terraform_remote_state`. Only `dev` exists.
- **The job sets no `command`.** The Dockerfile's `ENTRYPOINT` owns it, `args`
  picks the subcommand and is not ignored, and `ignore_changes` covers `image`
  and `env` only. Do not undo this.
- `make deploy` is az cli, not an apply. Anything in `common_env` (such as
  `STATE_CONTAINER_URL`) lands on a running job only through `make deploy`.

### Secrets ([secrets](docs/design.md#secrets))

- Terraform creates **no** `azurerm_key_vault_secret` and no native Key Vault
  reference. `make secrets` prints the commands to set them by hand.
- `settings.secret()` reads the environment, then `.env`, then Key Vault.
  Never commit a filled-in `.env`.
- `DEEPSEEK-API-KEY` and `DIGEST-EMAIL-TO` are optional. `DIGEST-EMAIL-TO` must
  be plain ASCII.

### Checks ([checks](docs/checks.md))

- **Checks are pure `(RepoSnapshot) -> list[Finding]` functions** with no I/O,
  tested by building a snapshot (`tests/factories.py`).
- **A tri-state's `None` stays silent**: `in_catalogue`,
  `code_scanning_alerts`, `open_issues`, `branches`, `renovate_pr_ever`,
  `required_checks`. In `required_checks`, an empty tuple is a claim, not an
  unknown.
- Repos with a topic in `ignore_topics` (`no-scan,tutorial`) are skipped but
  named in the digest.
- The `deploy-failed` / `build-failed` / `terraform-pending` / `deploy-pending` **label names
  are a contract** with jay-withers/workflows
  ([delivery](docs/checks.md#delivery-what-the-shared-workflows-are-waiting-on)).
- Lock files and workflows are read whole (`capped=False`). Only prompt-bound
  files are capped.
- **The scan runs Sunday 18:00 UTC, and `BACKLOG_MIN_AGE_DAYS` and
  `BLOCKED_PR_DAYS` are coupled to it.** Move one and the other moves with it
  ([schedule](docs/checks.md#the-scan-runs-on-sunday-and-that-is-load-bearing)).

### The model ([what it may do](docs/design.md#the-checks-and-what-the-model-is-allowed-to-do))

- `llm.py` contributes `summary`, `themes` and `suggestions` only. **The set of
  findings that goes in is the set that comes out.**
- Suggestions are never totalled, never in the subject line, and **never
  written to state**.
- Never send file paths of an alert, or workflow contents, to DeepSeek. It is
  China-hosted and may train on inputs. Use `deepseek-flash`, not the
  `deepseek-chat` alias.

### GitHub API ([GitHub API](docs/design.md#github-api))

- **No search API, ever.** Its secondary limit answers 403, which `fetch.py`
  retries.
- The detail query batches ten repos, with names passed as **variables**.
  Adding `branchProtectionRules` or `vulnerabilityAlerts` means widening the
  App first, or the whole batch fails.
- App auth is a hand-rolled RS256 JWT on `PyJWT[crypto]`. All App permissions
  are read-only.

### Shared code ([copied modules](docs/design.md#copied-modules), [Terraform](docs/design.md#terraform-conventions), [Docker](docs/design.md#docker))

- `fetch.py`, `mailer.py`, `telemetry.py` and `cli.py` are copied from
  market-agent. **Re-copy, don't diverge.** Secret resolution comes from
  jay-withers/appkit (`appkit[azure]`): fix a bug there, with its test, and
  let Renovate raise the bump. `settings.py` keeps `Settings` and the cached
  wrappers the tests clear.
- `scripts/check-tf-standards.sh` is shared verbatim with four repos. The lock
  file carries hashes for linux_amd64, linux_arm64 and darwin_arm64.
- The image runs as uid **10001**, numerically. Both `uv sync` calls use
  `--no-editable`. Don't trust logs from a QEMU-emulated image.

### CI ([CI](docs/design.md#ci))

Thin callers of `jay-withers/workflows`, pinned by SHA. The required checks
(set in github-repos) are `pre-commit / Pre-commit`, `test / Test`,
`terraform / Terraform`, `terraform-plan` and `container-build`. `ci-container-build` also checks the image's Python is the one `mise.toml` pins, which local runs and CI test on (workflows v1.35.0+, the image's `"python"` field); `mise.toml` is in its path filter, so a Python bump runs it. ci-python
needs `extras: dev`. `ci-go` runs the Go port as `go / Test`, which nothing
requires until the cutover renames its job to `test`. `cd-terraform` plans on merge and applies only when run
by hand. `cd-tag` → `cd-publish` deploys when app files changed.

## Testing

`make test` runs the Go suite (`make go-test`), then pytest. Tests never
touch the network or Azure. The Go tests follow the same rules as the Python:
`t.Chdir` into an empty directory, `KEY_VAULT_URI` set empty,
`settings.Reset()` and `github.ResetCache()` around each test, an RSA key
generated in process, HTTP faked with a `RoundTripper` and the clock frozen
through `model.Now`.

The Python's `tests/conftest.py`:

- **`chdir`s into an empty `tmp_path`**, so a developer's own `.env` cannot reach
  the suite. Both `Settings` and `secret()` resolve `.env` relative to the
  working directory, and deleting an environment variable does not stop either
  reading the file — which silently gave anyone with a `.env` a different test
  suite from CI.
- Sets every secret as an environment variable and **sets `KEY_VAULT_URI`
  empty rather than deleting it**, so a missing secret fails rather than falling
  through to a real call. An explicit empty value beats a `.env`; an absent one
  does not.
- Clears the `settings()`, `secret()`, `optional_secret()` and `dotenv()` caches
  around each test. HTTP is faked with
`httpx.MockTransport` via `tests/helpers.py`. `tests/` is a package, so helpers
import as `tests.helpers` — a bare `from conftest import ...` does not resolve.

**The test RSA key is generated in-process with `cryptography`.** Never commit
a PEM, even a throwaway, to a public repo. Reset `auth._cached` between tests.

The checks are pure `(RepoSnapshot) -> list[Finding]` functions, tested by
constructing a snapshot directly with no HTTP anywhere near the test. Keep them
that way.

`tests/factories.py` holds the snapshot builders, **not `tests/helpers.py`** —
that one is copied verbatim from market-agent and is re-copied rather than
diverged, so anything specific to this project's models belongs in factories.

`conftest.py` **deletes `DEEPSEEK_API_KEY`** alongside the others, so triage is
off unless a test switches it on. A test that enabled it accidentally would
reach `api.deepseek.com` for real.

CI gates three more things, all inside `test / Test`: mypy over `src/` and
`tests/` (`[tool.mypy]` in `pyproject.toml`, so `make typecheck` runs the same
check), ruff (`ruff check` and `ruff format --check`, the same ruff `make lint`
runs) and coverage. Total coverage of `repoagent` must be at least 90%, and so
must the lines a PR changes; the table is in the job summary, not a PR comment.
Don't meet either floor with a test that only asserts a mock; test the
behaviour.

## Commit messages

Conventional Commits, enforced by commitlint at commit-msg time. The
`no-commit-to-branch` hook blocks direct commits to `main`.

## Repo settings

Branch protection and repository settings live in
[jay-withers/github-repos](https://github.com/jay-withers/github-repos), whose
`terraform.tfvars` doubles as the catalogue of every repo. A change there takes
effect on `make apply`, not on merge — and applying a required check that
nothing reports leaves every PR *pending* rather than failing it.
