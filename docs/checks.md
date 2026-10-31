# The checks

Why each check behaves as it does: the traps behind the estate, security,
workflow, delivery and Renovate checks, and the schedule they depend on. Moved
out of CLAUDE.md, which keeps the rules and links here.

## Exempting a repository

**A repository carrying any topic in `ignore_topics` is skipped entirely** — not
fetched, not checked, not sent to the model. Default: `no-scan,tutorial`.

A topic rather than a list of repository names or a state entry, because
`github-repos` already applies topics from its catalogue, so the exemption is
declared where the rest of a repository's configuration lives. `no-scan` is the
explicit marker for anything; `tutorial` is semantic — teaching material
genuinely should not be held to infrastructure standards.

`git-demo` is the case this exists for, and its catalogue entry already said so
in a comment: *"it will show up in repo-agent's digest as missing both —
expected, not a defect"*. It carries `tutorial`, so it now drops out with no
change to either repository.

**Exempt repositories are named in the digest, not merely counted.** An
exemption nobody can see is one nobody revisits, and a topic added by mistake
would otherwise silently drop a repository out of the digest for ever.

## Security scanning: code scanning read, the rest not

**`security.open_code_scanning_alerts` reads open code scanning alerts from
every tool**: CodeQL, and Trivy's container image findings, which the shared
docker.yml uploads without failing a publish. The CodeQL workflow is green
whatever it finds and results land in a Security tab nobody
visits, so an alert can sit open for months behind a wall of passing checks.
That is exactly the "configured but not working" gap this agent exists for.

- **REST, per repository**, because GraphQL does not expose code scanning. One
  page of 100, `state=open`, every tool; a full page says "at least". The
  title names the tools (`CodeQL and Trivy`).
- **Needs the App's `Code scanning alerts: read` (`security_events`)
  permission**, which means an accepted re-approval on the installation.
  `auth.permitted` reads the token's own `permissions` grant and the fetch is
  skipped outright without it — probing would 403, and `fetch` retries 403 as a
  secondary rate limit, costing seconds per repository to learn nothing.
- **`code_scanning_alerts` is a tri-state.** `None` (no permission, a 404 because
  CodeQL has never run, any failure) stays silent; `()` is CodeQL having run and
  found nothing. A repository without CodeQL is not reported as clean.
- **Fetched independently of the detail query**, so a GraphQL failure does not
  cost the alerts, nor the reverse.
- **One finding per repository**, graded by the worst alert, naming rule ids and
  counts and **never file paths** — findings go to DeepSeek, and the location of
  an unfixed vulnerability is the part to keep out of that prompt.
  `test_the_detail_never_names_a_file` pins it.

**Dependabot and secret scanning alerts are still not read.** Surveyed across the
estate first: all 13 repositories already run gitleaks in pre-commit, every
Terraform repository runs Checkov, and 12 of 13 run pre-commit in CI *and* have
`pre-commit / Pre-commit` as a required check. Each would need another App
permission and re-approval.

**The residual gap, recorded rather than argued:** gitleaks in pre-commit is a
*local* control. It runs only for people who have run `pre-commit install`, and
`--no-verify` skips it — so a secret pushed from a fresh clone, from CI, or by
someone who skipped the hook never meets it. GitHub secret scanning is the
backstop for exactly that case, and nothing readable from a config file says
whether it has ever fired. Revisit if a credential ever does leak, or if the
estate stops being uniform.

## Checking an estate that is already declared

`jay-withers/github-repos` holds a catalogue of every repository and applies
branch protection, required checks, description and topics from it. So a check
asking "does this repo have branch protection" would mostly re-report that
repo's own Terraform. **The useful inversion is coverage** — `estate.unmanaged`
asks what exists on GitHub that the catalogue does not declare, because nothing
is enforcing anything on those.

`in_catalogue` is a **tri-state**. `None` means the catalogue could not be read
and must never render as "every repository is unmanaged", which is what a plain
boolean produces the first time the file moves. `catalogue_repo` empty switches
the check off entirely for the same reason.

`parse_catalogue` is a brace-depth scan, not a regex over the whole file: the
values contain nested blocks (`required_status_checks = [{ context = ... }]`)
and any pattern loose enough to find the repository keys also finds those. It is
deliberately not a real HCL parser — that would be a dependency for one check.

**A `.terraform.lock.hcl` never names a platform.** The first version of
`estate.incomplete_terraform_lock` searched for the literal `linux_amd64` and
consequently reported every Terraform repository in the estate, including this
one, whose lock is correct. Platform coverage is the **count of `h1:` hashes per
provider block** — one per platform locked — and `zh:` entries are the
registry's zip hashes, present regardless, so counting those proves nothing.
`test_platform_names_are_never_looked_for_in_a_lock_file` pins this.

**Lock files and workflows are read whole; only prompt-bound files are capped.**
`_blob_text` truncates at `MAX_FILE_BYTES` (8,000) by default, which once cut
terraform-root-aks's 9.7 KB lock inside its last block and reported a correct
lock as `tls (0 of 3)` every week. Workflows had the same problem: four in
`jay-withers/workflows` are over the cap. Neither ever reaches the model, so
both pass `capped=False`; README, Dockerfile and Renovate config keep the cap.

**`estate.unreportable_required_check` compares the two halves of that
catalogue.** It reads `required_status_checks` out of the same block the
repository names come from, and asks whether any workflow in the repository can
produce each context. The failure it exists for is the worst one branch
protection has, and a silent one: a required check that never reports leaves
every pull request *pending* rather than failing it, so nothing merges except by
bypassing the ruleset — azure-landingzone required `terraform / Terraform` while
its workflows reported `ci-terraform`, and its Renovate backlog reached six
before anyone noticed the merges had all been bypasses.

It is deliberately half a check, because `<caller job id> / <reusable job name>`
has one half in another repository. Only the caller job id is verified; a
renamed job inside `jay-withers/workflows` would still slip through. Verifying
the far half means fetching that repository's workflows too — worth doing only
if that failure ever actually happens. Job ids are read with a regex on
two-space indentation rather than with a YAML dependency, the same trade as
`parse_catalogue` not being an HCL parser.

**`estate.unenforced_check` is the same comparison run backwards**, and it is
the half that actually happens. A repository is created from the template with
every workflow in place, and its catalogue entry is written before anyone reads
the context names off a pull request — so CI runs, goes red, and the pull
request merges anyway because nothing in the ruleset is waiting on it. With
`autoApprove` and platform auto-merge, no human ever sees the red tick.

It only ever recommends a job that reports on **every** pull request, because a
required check that sometimes does not report is worse than no required check at
all — the exact failure the mirror check exists for. Three exclusions carry that:

- a workflow with no `pull_request` trigger, which can never report on one;
- a `pull_request` trigger carrying `paths`/`paths-ignore`, which is why this
  repository's own `ci-container-build` is silent here rather than recommended;
- a job with a matrix, whose context carries the leg (`plan (dev)`) and cannot
  be reconstructed from the workflow text.

Two kinds of coverage count as enforced, and both came from one digest that
reported 20 of the estate's 25 findings wrongly:

- **A job a required job `needs`, directly or transitively.** The estate's
  always-reporting gates (`container-build`, `terraform-plan`) are required
  *instead of* the gated jobs behind them (`changes`, `build`, `plan`), and fail
  when those fail. Recommending the job behind a gate is recommending exactly
  the check the gate exists to avoid. `needs` is parsed in all three forms.
- **CodeQL, when the catalogue sets `code_scanning = true`.** The ruleset's code
  scanning rule blocks merges on new alerts, which is how this estate enforces
  CodeQL; it is never a required status check. Without the flag the finding
  says to set it, not to require `codeql / …`.

Run against this repository's own workflows it produces nothing, which is
the test that matters: those exclusions are what make the difference between
four correct recommendations on a fresh repository and four wrong ones here.

`required_checks` is a **tri-state here too, and for a sharper reason than
elsewhere**: `None` (catalogue unreadable, or the repository is absent from it —
`estate.unmanaged` owns the second) must stay silent, while an **empty tuple is
a claim** — declared, requiring nothing — and is precisely the case this check
was written for. `unreportable_required_check`'s `if not required` conflates the
two harmlessly; this one must not.

Workflow **contents** are fetched, not just filenames, because whether an action
is pinned to a commit and which runner a job asks for are answered by the text
and nothing else. They are deliberately **not** sent to the triage prompt —
`llm._user_prompt` sends `workflow_names` — since a dozen workflow files per
repository would dominate it.

## Workflow, container and housekeeping checks

**`workflows.unscoped_token` accepts `permissions:` on every job as well as at
the top.** This repository's own `cd-tag.yml` has no top-level block, but every
job in it is scoped, which is just as tight. A check that only looked at column
0 would report the estate's own template. A single unscoped job is enough to
fire, because that job's token falls back to the repository default. A
workflow triggered **only** by `workflow_call` is skipped. A called workflow's
token is whatever its caller grants, and `jay-withers/workflows` leaves them
unscoped on purpose. The first survey of the estate reported three of them
until this exemption was added. Job
parsing is in `checks/workflow_text.py`, shared with `estate`. Extend it there
instead of writing a second regex.

`workflows.pull_request_target_checkout` fires only when the workflow **also**
references `github.event.pull_request.head`. A bare `pull_request_target` is
legitimate (labelling, commenting). Running the author's code with the base
repository's secrets is not.

`container.*` reads the **root `Dockerfile` only**, because that is the only
one fetched. Most of the estate passes both checks already, so they guard
against regressions in repositories that did not start from the template.

`hygiene.abandoned_prs` excludes Renovate's pull requests, which belong to
`stalled_prs`. That keeps any one pull request from being reported twice. It is
measured from `updatedAt`, not creation.

`branches` is a **tri-state**, like `in_catalogue`. `None` (the detail query did
not answer) stays silent. The page holds 50 refs, because GraphQL scores
*potential* nodes across the ten-repository batch. `branch_total` lets the check
say "at least" when the page does not hold every branch.

## Delivery: what the shared workflows are waiting on

`checks.delivery` reads open issues that jay-withers/workflows opens:
`deploy-failed` (a deploy or apply on main failed; the next success closes it),
`terraform-pending` (cd-terraform planned changes on main that nobody has
applied; applying is a manual run, which closes it) and `deploy-pending` (a
release held from deploying; running cd-publish with its tag deploys it and
closes it). Creating any of them emails the owner once, and the digest is the weekly reminder of whatever is still open.

- **The label names are a contract** with the workflows; the constants in
  `checks/delivery.py` must match them.
- **Issues come over REST** (`client.open_issues`, one page of 100 per repo),
  gated on the App's **Issues** permission exactly as code scanning is gated
  on `security_events`: without it nothing is requested and the checks stay
  silent. `/issues` lists pull requests too; `parse.issues` drops them.
- `open_issues` is a tri-state like `code_scanning_alerts`: `None` (no
  permission, or the request failed) must never read as "no failed deploys".

## Renovate PRs that cannot merge

`renovate.blocked_prs` names Renovate PRs whose `mergeStateStatus` has been
BLOCKED (a required check failing) or DIRTY (conflicts) for a week. It exists
because `stalled_prs` reports a *backlog*, which reads like a schedule problem,
while a blocked PR is broken: auto-merge will never take it, and Renovate
rebases it every week for ever. The lock-file maintenance PRs sat exactly like
this for a month in four repositories, failing pre-commit on a terraform-docs
rewrite, and every transitive dependency they carried, CVE fixes included,
stayed frozen. The digest said "not being merged" each week and never said why.

- `mergeStateStatus` needs only Pull requests: read. `statusCheckRollup` would
  say more but wants the Checks permission too, and a permission failure in
  GraphQL fails the whole detail batch.
- A week, because a freshly rebased PR reads BLOCKED while its checks run.
- `stalled_prs` skips blocked PRs, so each PR is reported once.

## Onboarded is not activated

`renovate.never_opened_a_pr` exists because the other four Renovate checks all
pass on a repository that has never received a single dependency update. A repo
created from the template carries a config (so `missing_config` passes), commits
it directly rather than onboarding (no PR for `onboarding_unmerged`), has
nothing open (`stalled_prs` sees nothing) and extends the shared preset
(`default_config_only` is satisfied). Everything is green and nothing has ever
been updated.

Four repositories were in that state at once — `gym-log`, `finances`,
`azure-container-apps` and `repo-agent` itself — each showing **onboarded**
rather than **activated** in Mend's portal, each with a full Dependency
Dashboard and seven updates parked under *Awaiting Schedule*. Renovate had run
and detected everything; it had simply never had a job land inside the preset's
`before 6am on monday` window. (That window is now all of Monday, for exactly
this reason: security-testing went months without one Renovate PR.)
`updateNotScheduled: true` means it rebases
existing branches at any hour, so only *creation* is confined to those six
hours a week — which is why market-agent's PRs were created at 00:55 and 05:34
but rebased at 16:06 and 19:01.

Two operator notes worth keeping:

- On the Dependency Dashboard, **`Create all awaiting schedule PRs at once` is
  the checkbox that works**. `Check this box to trigger a request for Renovate
  to run again` does not: the run re-evaluates the schedule, finds it is not
  Monday morning, and parks everything again. The existence of two separate
  checkboxes is the proof.
- `renovate_pr_ever` is a **tri-state**, for the same reason `in_catalogue` is.
  The closed-PR history is one 30-node page, so a repository with a long human
  history fills it with human pull requests. `None` means the page could not
  answer, and must never render as "Renovate has never run" — that would report
  the estate's busiest repositories as its deadest.

**The check only ever fires once per repository.** The moment Renovate opens its
first PR it goes quiet for good, so it catches a repository that never started,
not one that dies later — `stalled_prs` is the check for that. Dead-since-birth
is the case worth the finding, because nothing else in the estate will ever
mention it.

## The scan runs on Sunday, and that is load-bearing

The estate's shared preset (`github>jay-withers/renovate`) opens pull requests
`on monday` (it was `before 6am on monday` until October 2026) and throttles
them with `prHourlyLimit: 4`. The scan's
cron used to be `0 7 * * 1`, which meant **the agent observed every repository
at its weekly peak open-PR count**, one hour after a week of updates landed in
one burst.

`renovate.stalled_prs` reported market-agent every Monday because of this. Six
PRs were open at 07:00; all had been merged by `renovate[bot]` through platform
auto-merge by that evening, each within two minutes of Renovate rebasing it.
The spread that made it look manual is `strict_required_status_checks_policy`
on the ruleset: branches must be up to date, so one merge invalidates every
other open PR and the queue drains at one per Renovate run.

The cron is now `0 18 * * 0` — Sunday evening, the point of **maximum** drain,
six days after the burst rather than one hour into it. A Renovate PR still open
then has genuinely failed to merge, which is the only thing this check ever
wanted to know. The digest still arrives for Monday morning. Two side effects
worth knowing:

- **Sunday is off-peak for DeepSeek**, which bills peak rates on weekdays only,
  so triage now costs half what it did. That was not the reason to move and is
  not a reason to move back.
- **`BACKLOG_MIN_AGE_DAYS` is coupled to the cron.** At `0 7 * * 1` it had to be
  a week or every healthy repository read as backlogged; on Sunday it is 2,
  because a batch that never merged is six days old when the scan sees it.
  Move the cron and this number moves with it, in the same direction — too high
  on a Sunday scan and a week's failed batch is missed entirely.

The count itself is measured against the preset's `prConcurrentLimit: 20`,
**not** Renovate's default of 10.
