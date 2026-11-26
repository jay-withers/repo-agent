package github

import (
	"fmt"
	"net/http"
	"regexp"
	"strconv"
	"strings"

	"github.com/jay-withers/repo-agent/internal/fetch"
	"github.com/jay-withers/repo-agent/internal/jv"
	"github.com/jay-withers/repo-agent/internal/logging"
	"github.com/jay-withers/repo-agent/internal/settings"
)

var clientLog = logging.Named("repoagent.github.client")

// PageSize is GitHub's maximum, so listing the estate is one request.
const PageSize = 100

// MaxFileChars is enough of a file to judge it by, and a ceiling on what one
// repository contributes to the triage prompt. Counted in characters, as
// Python's slicing was.
const MaxFileChars = 8000

// DetailBatchSize is repositories per GraphQL request. GraphQL scores the
// potential node count of the whole document, so aliasing every repository
// at once fails on a large installation rather than degrading.
const DetailBatchSize = 10

// RenovateConfigPaths in Renovate's own precedence.
var RenovateConfigPaths = []string{
	"renovate.json",
	"renovate.json5",
	".github/renovate.json",
	".github/renovate.json5",
	".renovaterc",
	".renovaterc.json",
}

// InstallationRepos is every repository the App is installed on, paging only
// past 100. total_count is the stopping condition.
func InstallationRepos(client *http.Client) ([]map[string]any, error) {
	var repos []map[string]any
	for page := 1; ; page++ {
		headers, err := AuthHeaders(client)
		if err != nil {
			return nil, err
		}
		raw, err := fetch.GetJSON(client, settings.Get().GitHubAPIURL+"/installation/repositories", headers,
			fetch.Params{{"per_page", strconv.Itoa(PageSize)}, {"page", strconv.Itoa(page)}})
		if err != nil {
			return nil, err
		}
		payload := jv.Obj(raw)
		batch := jv.List(payload["repositories"])
		for _, r := range batch {
			if m := jv.Obj(r); m != nil {
				repos = append(repos, m)
			}
		}
		if len(repos) >= jv.IntOr(payload, "total_count", len(repos)) || len(batch) == 0 {
			break
		}
	}
	clientLog.Info(fmt.Sprintf("installation covers %d repositories", len(repos)))
	return repos, nil
}

// repoFragment is one alias per repository. Byte-for-byte the Python
// client's _REPO_FRAGMENT, comments included, so the query GitHub scores is
// the one already known to fit.
//
// Deliberately absent: branchProtectionRules and vulnerabilityAlerts. Both
// need permissions beyond the App's read-only set, and GraphQL reports a
// permission failure as an `errors` entry that fails the whole batch.
const repoFragment = `
  r%[1]d: repository(owner: %[2]s, name: %[3]s) {
    nameWithOwner
    defaultBranchRef { name }
    %[4]s
    workflows: object(expression: "HEAD:.github/workflows") {
      ... on Tree {
        entries {
          name
          # The text, not just the name: whether an action is pinned to a commit
          # and which runner a job asks for are both answered by the contents and
          # by nothing else. Workflow files are small; a README is the thing worth
          # being careful about.
          object { ... on Blob { text } }
        }
      }
    }
    pullRequests(states: OPEN, first: 30, orderBy: {field: CREATED_AT, direction: ASC}) {
      totalCount
      nodes {
        number
        title
        url
        isDraft
        createdAt
        updatedAt
        author { login }
        # For ` + "`renovate.blocked_prs`" + `: BLOCKED is a required check failing (or
        # still running), DIRTY is a conflict. Needs only Pull requests: read,
        # unlike statusCheckRollup, which wants the Checks permission too.
        mergeStateStatus
      }
    }
    # Whether Renovate has ever opened a pull request here at all, which is a
    # different question from whether one is open now — a repository that has
    # never received a single update looks identical to a fully merged one if
    # you only count what is open.
    #
    # Ordered ASC rather than DESC: Renovate opens its first PR within days of
    # onboarding, so the *earliest* pull requests are where the evidence is,
    # and a repository with a long human history would push it off a DESC page.
    # ` + "`totalCount`" + ` is what makes the answer honest — where it exceeds the page,
    # absence proves nothing and ` + "`parse`" + ` returns None rather than False.
    # Author is the only field needed; nothing here is ever rendered.
    closedPullRequests: pullRequests(
      states: [MERGED, CLOSED], first: 30, orderBy: {field: CREATED_AT, direction: ASC}
    ) {
      totalCount
      nodes { author { login } }
    }
    # One page of branches for ` + "`hygiene.stale_branches`" + `. Fifty rather than a
    # hundred because GraphQL scores the *potential* node count — 50 refs, each
    # with an open-PR count, across a ten-repository batch is comfortably
    # inside the limit; ` + "`totalCount`" + ` lets the check say "at least" when it is
    # not the whole list.
    branches: refs(refPrefix: "refs/heads/", first: 50) {
      totalCount
      nodes {
        name
        target { ... on Commit { committedDate } }
        associatedPullRequests(states: OPEN) { totalCount }
      }
    }
    releases(first: 1, orderBy: {field: CREATED_AT, direction: DESC}) {
      nodes { tagName publishedAt }
    }
  }
`

// fileSelection pulls each candidate file's text: one alias per path,
// because GraphQL has no "first of these that exists".
func fileSelection() string {
	var parts []string
	for i, path := range RenovateConfigPaths {
		parts = append(parts, fmt.Sprintf(`renovate%d: object(expression: "HEAD:%s") { ... on Blob { text } }`, i, path))
	}
	parts = append(parts,
		`readme: object(expression: "HEAD:README.md") { ... on Blob { text } }`,
		`tflock: object(expression: "HEAD:terraform/.terraform.lock.hcl") { ... on Blob { text } }`,
		`tflock_root: object(expression: "HEAD:.terraform.lock.hcl") { ... on Blob { text } }`,
		`dockerfile: object(expression: "HEAD:Dockerfile") { ... on Blob { text } }`,
	)
	return strings.Join(parts, "\n    ")
}

// DetailQuery is the batched query and its variables for one batch. Names
// go in as variables, never interpolated: `") { ... }` is a valid repository
// name and would otherwise rewrite the query.
func DetailQuery(batch []string) (string, map[string]any) {
	files := fileSelection()
	variables := map[string]any{}
	var fragments, signature []string
	for i, fullName := range batch {
		owner, name, _ := strings.Cut(fullName, "/")
		variables[fmt.Sprintf("o%d", i)] = owner
		variables[fmt.Sprintf("n%d", i)] = name
		fragments = append(fragments, fmt.Sprintf(repoFragment, i, fmt.Sprintf("$o%d", i), fmt.Sprintf("$n%d", i), files))
		signature = append(signature, fmt.Sprintf("$o%d: String!, $n%d: String!", i, i))
	}
	query := fmt.Sprintf("query RepoDetails(%s) {\n%s\n}", strings.Join(signature, ", "), strings.Join(fragments, ""))
	return query, variables
}

// RepoDetails fetches the detail behind each repository, keyed by
// owner/name. A repository whose alias comes back null is omitted, not an
// error: losing one repository's detail degrades its findings, losing the
// run reports nothing about any of them.
func RepoDetails(client *http.Client, fullNames []string) (map[string]map[string]any, error) {
	details := map[string]map[string]any{}
	for start := 0; start < len(fullNames); start += DetailBatchSize {
		batch := fullNames[start:min(start+DetailBatchSize, len(fullNames))]
		query, variables := DetailQuery(batch)
		headers, err := AuthHeaders(client)
		if err != nil {
			return nil, err
		}
		raw, err := fetch.GraphQL(client, settings.Get().GitHubGraphQLURL, query, variables, headers)
		if err != nil {
			return nil, err
		}
		data := jv.Obj(raw)
		for i, fullName := range batch {
			if node := jv.Obj(data[fmt.Sprintf("r%d", i)]); len(node) > 0 {
				details[fullName] = node
			} else {
				clientLog.Warn("no detail returned for " + fullName)
			}
		}
	}
	clientLog.Info(fmt.Sprintf("fetched detail for %d repositories", len(details)))
	return details, nil
}

// The App permissions GitHub calls "Code scanning alerts" and "Issues".
const (
	CodeScanningPermission = "security_events"
	IssuesPermission       = "issues"
)

// perRepo reads one REST page per repository. Without the permission it
// requests nothing and returns an empty map; a repository whose page could
// not be read maps to nil, which the checks treat as "could not tell".
func perRepo(client *http.Client, fullNames []string, permission, path, logMissing, logFailed string) (map[string][]any, error) {
	ok, err := Permitted(client, permission)
	if err != nil {
		return nil, err
	}
	if !ok {
		clientLog.Info(logMissing)
		return map[string][]any{}, nil
	}
	out := map[string][]any{}
	for _, fullName := range fullNames {
		headers, err := AuthHeaders(client)
		if err != nil {
			return nil, err
		}
		raw, err := fetch.GetJSON(client, fmt.Sprintf("%s/repos/%s/%s", settings.Get().GitHubAPIURL, fullName, path), headers,
			fetch.Params{{"state", "open"}, {"per_page", strconv.Itoa(PageSize)}})
		if err != nil {
			// Info, not warning: a 404 from a repository that has never run a
			// scanner is the normal case for much of an estate.
			clientLog.Info(fmt.Sprintf(logFailed, fullName, err))
			out[fullName] = nil
			continue
		}
		page, isList := raw.([]any)
		if !isList {
			out[fullName] = nil
			continue
		}
		if page == nil {
			page = []any{}
		}
		out[fullName] = page
	}
	return out, nil
}

// CodeScanningAlerts is open code scanning alerts (every tool) for each
// repository, one page of 100.
func CodeScanningAlerts(client *http.Client, fullNames []string) (map[string][]any, error) {
	return perRepo(client, fullNames, CodeScanningPermission, "code-scanning/alerts",
		"App lacks the security_events permission; not reading code scanning alerts",
		"no code scanning alerts for %s: %s")
}

// OpenIssues is open issues (pull requests included; parse drops them) for
// each repository, one page of 100.
func OpenIssues(client *http.Client, fullNames []string) (map[string][]any, error) {
	return perRepo(client, fullNames, IssuesPermission, "issues",
		"App lacks the issues permission; not reading issues",
		"issues unavailable for %s: %s")
}

// CatalogueEntry is what the catalogue's ruleset enforces on one repository.
type CatalogueEntry struct {
	RequiredChecks map[string]bool
	CodeScanning   bool
}

// Catalogue is every repository github-repos declares. **nil and empty mean
// different things**: nil is "could not be read", which must never render as
// "every repository is unmanaged".
func Catalogue(client *http.Client) map[string]CatalogueEntry {
	cfg := settings.Get()
	if cfg.CatalogueRepo == "" {
		return nil
	}
	owner, name, _ := strings.Cut(cfg.CatalogueRepo, "/")
	query := `
    query Catalogue($owner: String!, $name: String!, $path: String!) {
      repository(owner: $owner, name: $name) {
        object(expression: $path) { ... on Blob { text } }
      }
    }
    `
	headers, err := AuthHeaders(client)
	if err != nil {
		clientLog.Warn(fmt.Sprintf("could not read the catalogue at %s: %s", cfg.CatalogueRepo, err))
		return nil
	}
	raw, err := fetch.GraphQL(client, cfg.GitHubGraphQLURL, query,
		map[string]any{"owner": owner, "name": name, "path": "HEAD:" + cfg.CataloguePath}, headers)
	if err != nil {
		clientLog.Warn(fmt.Sprintf("could not read the catalogue at %s: %s", cfg.CatalogueRepo, err))
		return nil
	}
	text, ok := jv.Obj(jv.Obj(jv.Obj(raw)["repository"])["object"])["text"].(string)
	if !ok {
		clientLog.Warn(fmt.Sprintf("no catalogue file at %s:%s", cfg.CatalogueRepo, cfg.CataloguePath))
		return nil
	}
	entries := ParseCatalogueEntries(text)
	clientLog.Info(fmt.Sprintf("catalogue declares %d repositories", len(entries)))
	return entries
}

var (
	reposOpen   = regexp.MustCompile(`^repos\s*=\s*\{`)
	repoKey     = regexp.MustCompile(`^"([^"]+)"\s*=\s*\{\s*$`)
	contextLine = regexp.MustCompile(`\bcontext\s*=\s*"([^"]+)"`)
	codeScanOn  = regexp.MustCompile(`^code_scanning\s*=\s*true\b`)
)

// ParseCatalogueEntries reads each repository directly inside the tfvars
// `repos = { ... }` block, with its required contexts and code_scanning
// flag. A brace-depth scan, not an HCL parser: it reads what the catalogue
// looks like and ignores what it does not recognise.
func ParseCatalogueEntries(text string) map[string]CatalogueEntry {
	entries := map[string]CatalogueEntry{}
	current := ""
	depth := 0
	inRepos := false
	for _, raw := range splitLines(text) {
		line, _, _ := strings.Cut(raw, "#")
		line = strings.TrimSpace(line)
		if line == "" {
			continue
		}
		if !inRepos {
			if reposOpen.MatchString(line) {
				inRepos, depth = true, 1
			}
			continue
		}
		if m := repoKey.FindStringSubmatch(line); depth == 1 && m != nil {
			current = m[1]
			entries[current] = CatalogueEntry{RequiredChecks: map[string]bool{}}
		} else if current != "" {
			if c := contextLine.FindStringSubmatch(line); c != nil {
				entries[current].RequiredChecks[c[1]] = true
			} else if codeScanOn.MatchString(line) {
				e := entries[current]
				e.CodeScanning = true
				entries[current] = e
			}
		}
		depth += strings.Count(line, "{") - strings.Count(line, "}")
		if depth <= 1 {
			current = ""
		}
		if depth <= 0 {
			break
		}
	}
	return entries
}
