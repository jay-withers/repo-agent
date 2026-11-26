# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo does

One Go binary (`cmd/repoagent`, packages in `internal/`), one distroless
image, one Container Apps job. It reads
every GitHub repository the App is installed on, checks Renovate is actually
working, and emails a weekly digest that is waiting on Monday morning.

`README.md` covers *why* and how to run it. This file is the rules, one line
each. The reasoning behind them, and the incidents that made them, are in
[docs/design.md](docs/design.md) and [docs/checks.md](docs/checks.md): read the
linked section before changing the thing a rule protects.

It was Python until November 2026, and was ported with no visible change
([Go port](docs/design.md#the-go-port)).

**Status: working.** It authenticates, lists every repository, fetches detail
for each with one batched GraphQL query, runs deterministic checks, asks
DeepSeek to triage the results, and emails a digest.

## Commands

```bash
make install   # mise tools (Go, golangci-lint, Terraform tools) + pre-commit hooks
make test      # gofmt, golangci-lint, go test -race: no network, no Azure, no credentials
make typecheck # go vet
make lint      # every pre-commit hook
make run       # repoagent render: scans real GitHub, prints the digest, sends nothing
make validate  # terraform init -backend=false + validate
make deploy IMAGE_TAG=vX.Y.Z
make state     # print what the scan remembers between runs
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

### Output ([Go port](docs/design.md#the-go-port))

- **`testdata/golden` pins every visible output**: digest subject, text and
  HTML, the DeepSeek prompt, the GraphQL query, the state document. The Python
  wrote them; nothing regenerates them now. A change to one of those outputs
  edits its golden file in the same PR, deliberately, and the PR says so.
- Output formatting goes through `internal/pyfmt` (`repr` for quoted titles,
  `{:,}`, `splitlines`, `html.escape`, `json.dumps`), never Go's look-alikes:
  they differ in corners the golden files catch.
- Logs keep the Python line format and logger names (`internal/logging`), not
  JSON: obsview groups errors by masking quoted values.

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
- `settings.Secret()` reads the environment, then `.env`, then Key Vault, and
  a secret the vault lacks is an error. Never commit a filled-in `.env`.
- `DEEPSEEK-API-KEY` and `DIGEST-EMAIL-TO` are optional. `DIGEST-EMAIL-TO` must
  be plain ASCII.

### Checks ([checks](docs/checks.md))

- **Checks are pure `func(model.RepoSnapshot) []model.Finding`** with no I/O
  (`internal/checks`), tested by building a snapshot.
- **A tri-state's nil stays silent**: `InCatalogue`, `CodeScanningAlerts`,
  `OpenIssues`, `Branches`, `RenovatePREver`, `RequiredChecks` (pointers). In
  `RequiredChecks`, a pointer to an empty slice is a claim, not an unknown.
- Repos with a topic in `ignore_topics` (`no-scan,tutorial`) are skipped but
  named in the digest.
- The `deploy-failed` / `build-failed` / `terraform-pending` / `deploy-pending` **label names
  are a contract** with jay-withers/workflows
  ([delivery](docs/checks.md#delivery-what-the-shared-workflows-are-waiting-on)).
- Lock files and workflows are read whole (`blobText(..., false)`). Only
  prompt-bound files are capped.
- **The scan runs Sunday 18:00 UTC, and `BacklogMinAgeDays` and
  `BlockedPRDays` are coupled to it.** Move one and the other moves with it
  ([schedule](docs/checks.md#the-scan-runs-on-sunday-and-that-is-load-bearing)).

### The model ([what it may do](docs/design.md#the-checks-and-what-the-model-is-allowed-to-do))

- `internal/llm` contributes `summary`, `themes` and `suggestions` only. **The set of
  findings that goes in is the set that comes out.**
- Suggestions are never totalled, never in the subject line, and **never
  written to state**.
- Never send file paths of an alert, or workflow contents, to DeepSeek. It is
  China-hosted and may train on inputs. Use `deepseek-flash`, not the
  `deepseek-chat` alias.

### GitHub API ([GitHub API](docs/design.md#github-api))

- **No search API, ever.** Its secondary limit answers 403, which
  `internal/fetch` retries.
- The detail query batches ten repos, with names passed as **variables**.
  Adding `branchProtectionRules` or `vulnerabilityAlerts` means widening the
  App first, or the whole batch fails.
- App auth is a hand-rolled RS256 JWT on golang-jwt (v5). All App
  permissions are read-only.

### Shared code ([shared code](docs/design.md#shared-code), [Terraform](docs/design.md#terraform-conventions), [Docker](docs/design.md#docker))

- `internal/settings` ports appkit's secret resolution, and `internal/fetch`
  and `internal/mailer` started as market-agent's Python. Nothing else in the
  estate is Go yet, so they are this repo's own; a second Go app is the
  trigger to make them a shared module.
- `scripts/check-tf-standards.sh` is shared verbatim with four repos. The lock
  file carries hashes for linux_amd64, linux_arm64 and darwin_arm64.
- The image is a static binary on distroless, uid **10001** numerically. The
  builder's `golang` tag is exactly `mise.toml`'s `go` (Renovate moves both).

### CI ([CI](docs/design.md#ci))

Thin callers of `jay-withers/workflows`, pinned by SHA. The required checks
(set in github-repos) are `pre-commit / Pre-commit`, `test / Test`,
`terraform / Terraform`, `terraform-plan` and `container-build`. `test / Test`
is ci-go (workflows' `go.yml`, job id `test`). CodeQL analyses `go` (autobuilt
with mise's Go) and `actions`. `cd-terraform` plans on merge and applies only
when run by hand. `cd-tag` → `cd-publish` deploys when app files changed.

## Testing

`make test`. Tests never touch the network or Azure:

- **`t.Chdir` into an empty `t.TempDir()`**, so a developer's own `.env`
  cannot reach the suite: settings and secrets both read `.env` from the
  working directory.
- **`KEY_VAULT_URI` set empty**, and `settings.VaultGetter` replaced where a
  test needs a vault, so a missing secret fails rather than reaching Azure.
- `settings.Reset()` and `github.ResetCache()` around each test; the clock is
  `model.Now`, frozen where an age matters; `fetch.Sleep` replaced so retries
  don't wait.
- HTTP is faked with an `http.RoundTripper` on the `*http.Client` every call
  takes. `DEEPSEEK_API_KEY` is unset unless a test switches triage on.
- **The test RSA key is generated in process.** Never commit a PEM, even a
  throwaway, to a public repo (gitleaks flags even a fake `BEGIN PRIVATE KEY`).

The checks are pure functions, tested by constructing a snapshot directly with
no HTTP anywhere near the test. The golden test (`internal/scan`) drives the
whole scan from `testdata/golden/*/fixture.json`. Keep `testdata/golden`
excluded from the whitespace hooks: its files are compared byte for byte.

CI gates more inside `test / Test`: gofmt, vet, golangci-lint,
`go mod tidy -diff`, `-race`, and coverage. Total coverage of the module must
be at least 90%, and so must the lines a PR changes; the table is in the job
summary. Don't meet either floor with a test that only asserts a mock; test
the behaviour.

## Commit messages

Conventional Commits, enforced by commitlint at commit-msg time. The
`no-commit-to-branch` hook blocks direct commits to `main`.

## Repo settings

Branch protection and repository settings live in
[jay-withers/github-repos](https://github.com/jay-withers/github-repos), whose
`terraform.tfvars` doubles as the catalogue of every repo. A change there takes
effect on `make apply`, not on merge — and applying a required check that
nothing reports leaves every PR *pending* rather than failing it.
