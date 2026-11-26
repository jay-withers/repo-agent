package github

import (
	"bytes"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"io"
	"net/http"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/golang-jwt/jwt/v5"

	"github.com/jay-withers/repo-agent/internal/fetch"
	"github.com/jay-withers/repo-agent/internal/settings"
)

var (
	keyOnce sync.Once
	key     *rsa.PrivateKey
)

// testKey is generated in process: never a committed PEM.
func testKey(t *testing.T) (*rsa.PrivateKey, string) {
	keyOnce.Do(func() {
		var err error
		if key, err = rsa.GenerateKey(rand.Reader, 2048); err != nil {
			t.Fatal(err)
		}
	})
	der, _ := x509.MarshalPKCS8PrivateKey(key)
	return key, string(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}))
}

func setup(t *testing.T) *rsa.PrivateKey {
	t.Helper()
	k, p := testKey(t)
	t.Chdir(t.TempDir())
	t.Setenv("KEY_VAULT_URI", "")
	t.Setenv("GITHUB_APP_ID", "123456")
	t.Setenv("GITHUB_APP_PRIVATE_KEY", p)
	settings.Reset()
	ResetCache()
	restore := fetch.Sleep
	fetch.Sleep = func(time.Duration) {}
	t.Cleanup(func() { settings.Reset(); ResetCache(); fetch.Sleep = restore })
	return k
}

type route func(*http.Request) (int, any)

type fake struct {
	routes map[string]route
	calls  []string
}

func (f *fake) RoundTrip(req *http.Request) (*http.Response, error) {
	f.calls = append(f.calls, req.Method+" "+req.URL.Path)
	for prefix, r := range f.routes {
		if strings.HasPrefix(req.Method+" "+req.URL.Path, prefix) {
			code, body := r(req)
			data, _ := json.Marshal(body)
			return &http.Response{StatusCode: code, Body: io.NopCloser(bytes.NewReader(data)), Header: http.Header{}}, nil
		}
	}
	return &http.Response{StatusCode: 404, Body: io.NopCloser(strings.NewReader("{}")), Header: http.Header{}}, nil
}

func ok(body any) route { return func(*http.Request) (int, any) { return 200, body } }

func tokenRoutes(perms map[string]any, expires any) map[string]route {
	return map[string]route{
		"GET /app/installations": ok([]any{map[string]any{"id": 7}}),
		"POST /app/installations/7/access_tokens": func(*http.Request) (int, any) {
			return 201, map[string]any{"token": "ghs_x", "expires_at": expires, "permissions": perms}
		},
	}
}

func TestAppJWTIsSignedByTheAppKeyAndBackdated(t *testing.T) {
	k := setup(t)
	at := time.Unix(1_800_000_000, 0)
	signed, err := AppJWT(at)
	if err != nil {
		t.Fatal(err)
	}
	claims := jwt.MapClaims{}
	_, err = jwt.ParseWithClaims(signed, claims, func(*jwt.Token) (any, error) { return &k.PublicKey, nil },
		jwt.WithValidMethods([]string{"RS256"}), jwt.WithoutClaimsValidation())
	if err != nil {
		t.Fatal(err)
	}
	if claims["iss"] != "123456" || claims["iat"].(float64) != 1_800_000_000-60 || claims["exp"].(float64) != 1_800_000_000+540 {
		t.Errorf("claims %v", claims)
	}
}

func TestAppJWTRejectsABadKey(t *testing.T) {
	setup(t)
	t.Setenv("GITHUB_APP_PRIVATE_KEY", "-----BEGIN NOT A KEY-----")
	settings.Reset()
	if _, err := AppJWT(time.Now()); err == nil || !strings.Contains(err.Error(), "not an RSA PEM") {
		t.Errorf("got %v", err)
	}
	t.Setenv("GITHUB_APP_ID", "")
	settings.Reset()
	if _, err := AppJWT(time.Now()); err == nil {
		t.Error("a missing App id is an error")
	}
}

func TestTokenIsCachedUntilNearExpiry(t *testing.T) {
	setup(t)
	f := &fake{routes: tokenRoutes(map[string]any{"issues": "read"}, time.Now().Add(time.Hour).UTC().Format(time.RFC3339))}
	client := &http.Client{Transport: f}
	for range 3 {
		if tok, err := Token(client); err != nil || tok != "ghs_x" {
			t.Fatalf("got %q %v", tok, err)
		}
	}
	if len(f.calls) != 2 {
		t.Errorf("minted more than once: %v", f.calls)
	}
	if yes, _ := Permitted(client, "issues"); !yes {
		t.Error("issues is granted")
	}
	if yes, _ := Permitted(client, "security_events"); yes {
		t.Error("security_events is not")
	}
	headers, _ := AuthHeaders(client)
	if headers["Authorization"] != "Bearer ghs_x" || headers["X-GitHub-Api-Version"] != "2022-11-28" {
		t.Errorf("headers %v", headers)
	}

	// Two minutes from expiry it mints again.
	now = func() time.Time { return time.Now().Add(59 * time.Minute) }
	t.Cleanup(func() { now = time.Now })
	if _, err := Token(client); err != nil {
		t.Fatal(err)
	}
	if len(f.calls) != 4 {
		t.Errorf("did not re-mint near expiry: %v", f.calls)
	}
}

func TestUnparseableExpiryAssumesAnHour(t *testing.T) {
	from := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	if got := parseExpiry("tomorrow", from); !got.Equal(from.Add(time.Hour)) {
		t.Errorf("got %v", got)
	}
	if got := parseExpiry(nil, from); !got.Equal(from.Add(time.Hour)) {
		t.Errorf("got %v", got)
	}
	if got := parseExpiry("2026-01-01T05:00:00Z", from); got.Hour() != 5 {
		t.Errorf("got %v", got)
	}
}

func TestInstallationIDNeedsOneInstallation(t *testing.T) {
	setup(t)
	none := &http.Client{Transport: &fake{routes: map[string]route{"GET /app/installations": ok([]any{})}}}
	if _, err := InstallationID(none, "jwt"); err == nil || !strings.Contains(err.Error(), "no installations") {
		t.Errorf("got %v", err)
	}
	two := &http.Client{Transport: &fake{routes: map[string]route{"GET /app/installations": ok([]any{
		map[string]any{"id": 1, "account": map[string]any{"login": "a"}}, map[string]any{"id": 2},
	})}}}
	if id, err := InstallationID(two, "jwt"); err != nil || id != 1 {
		t.Errorf("takes the first: %d %v", id, err)
	}
	noID := &http.Client{Transport: &fake{routes: map[string]route{"GET /app/installations": ok([]any{map[string]any{}})}}}
	if _, err := InstallationID(noID, "jwt"); err == nil {
		t.Error("an installation with no id is an error")
	}
	broken := &http.Client{Transport: &fake{}}
	if _, err := InstallationID(broken, "jwt"); err == nil {
		t.Error("a 404 is an error")
	}
	if _, err := Token(broken); err == nil {
		t.Error("Token reports the failure")
	}
	if _, err := Permitted(broken, "issues"); err == nil {
		t.Error("Permitted reports the failure")
	}
}

func TestTokenWithoutATokenIsAnError(t *testing.T) {
	setup(t)
	routes := tokenRoutes(nil, nil)
	routes["POST /app/installations/7/access_tokens"] = ok(map[string]any{})
	if _, err := Token(&http.Client{Transport: &fake{routes: routes}}); err == nil {
		t.Error("expected an error")
	}
}

func TestInstallationReposPagesByTotalCount(t *testing.T) {
	setup(t)
	page := 0
	routes := tokenRoutes(nil, "2099-01-01T00:00:00Z")
	routes["GET /installation/repositories"] = func(r *http.Request) (int, any) {
		page++
		if r.URL.Query().Get("page") != map[int]string{1: "1", 2: "2"}[page] || r.URL.Query().Get("per_page") != "100" {
			t.Errorf("page %d asked %s", page, r.URL.RawQuery)
		}
		batch := []any{map[string]any{"name": "a"}, "not an object"}
		return 200, map[string]any{"total_count": 2, "repositories": batch}
	}
	repos, err := InstallationRepos(&http.Client{Transport: &fake{routes: routes}})
	if err != nil || len(repos) != 2 || page != 2 {
		t.Errorf("%d repos over %d pages, %v", len(repos), page, err)
	}
}

func TestPerRepoReadsNothingWithoutThePermission(t *testing.T) {
	setup(t)
	f := &fake{routes: tokenRoutes(map[string]any{"metadata": "read"}, "2099-01-01T00:00:00Z")}
	alerts, err := CodeScanningAlerts(&http.Client{Transport: f}, []string{"o/a"})
	if err != nil || len(alerts) != 0 {
		t.Errorf("got %v %v", alerts, err)
	}
	for _, c := range f.calls {
		if strings.Contains(c, "code-scanning") {
			t.Errorf("asked anyway: %s", c)
		}
	}
}

func TestPerRepoKeepsUnreadableAsNil(t *testing.T) {
	setup(t)
	routes := tokenRoutes(map[string]any{"issues": "read"}, "2099-01-01T00:00:00Z")
	routes["GET /repos/o/list/issues"] = ok([]any{})
	routes["GET /repos/o/object/issues"] = ok(map[string]any{})
	issues, err := OpenIssues(&http.Client{Transport: &fake{routes: routes}}, []string{"o/list", "o/object", "o/missing"})
	if err != nil {
		t.Fatal(err)
	}
	if issues["o/list"] == nil || len(issues["o/list"]) != 0 {
		t.Errorf("an empty page is a fact: %v", issues["o/list"])
	}
	if issues["o/object"] != nil || issues["o/missing"] != nil {
		t.Errorf("unreadable is nil: %v", issues)
	}
}

func TestRepoDetailsBatchesTenAndSkipsNullAliases(t *testing.T) {
	setup(t)
	var batches []map[string]any
	routes := tokenRoutes(nil, "2099-01-01T00:00:00Z")
	routes["POST /graphql"] = func(r *http.Request) (int, any) {
		var body map[string]any
		_ = json.NewDecoder(r.Body).Decode(&body)
		vars := body["variables"].(map[string]any)
		batches = append(batches, vars)
		data := map[string]any{}
		for k := range vars {
			if strings.HasPrefix(k, "o") && k != "o3" {
				data["r"+k[1:]] = map[string]any{"nameWithOwner": "x"}
			}
		}
		return 200, map[string]any{"data": data}
	}
	var names []string
	for i := range 12 {
		names = append(names, "own/r"+string(rune('a'+i)))
	}
	details, err := RepoDetails(&http.Client{Transport: &fake{routes: routes}}, names)
	if err != nil {
		t.Fatal(err)
	}
	if len(batches) != 2 || len(batches[0]) != 20 || len(batches[1]) != 4 {
		t.Errorf("batches %v", batches)
	}
	if len(details) != 11 || details["own/rd"] != nil {
		t.Errorf("r3 of each batch is null and omitted: %d details", len(details))
	}
	// A name is a variable, never part of the document.
	query, vars := DetailQuery([]string{`o/") { evil }`})
	if strings.Contains(query, "evil") || vars["n0"] != `") { evil }` {
		t.Error("the name leaked into the query")
	}
	routes["POST /graphql"] = func(*http.Request) (int, any) { return 502, map[string]any{} }
	if _, err := RepoDetails(&http.Client{Transport: &fake{routes: routes}}, names); err == nil {
		t.Error("a failed batch is an error")
	}
}

func TestCatalogue(t *testing.T) {
	setup(t)
	routes := tokenRoutes(nil, "2099-01-01T00:00:00Z")
	routes["POST /graphql"] = ok(map[string]any{"data": map[string]any{"repository": map[string]any{"object": map[string]any{
		"text": "repos = {\n  \"a\" = {\n    code_scanning = true\n  }\n}\n",
	}}}})
	cat := Catalogue(&http.Client{Transport: &fake{routes: routes}})
	if len(cat) != 1 || !cat["a"].CodeScanning {
		t.Errorf("got %v", cat)
	}

	routes["POST /graphql"] = ok(map[string]any{"data": map[string]any{"repository": nil}})
	if Catalogue(&http.Client{Transport: &fake{routes: routes}}) != nil {
		t.Error("no file is nil, not empty")
	}
	routes["POST /graphql"] = func(*http.Request) (int, any) { return 401, map[string]any{} }
	if Catalogue(&http.Client{Transport: &fake{routes: routes}}) != nil {
		t.Error("an unreadable catalogue is nil")
	}
	if Catalogue(&http.Client{Transport: &fake{}}) != nil {
		t.Error("no token is nil")
	}
	t.Setenv("CATALOGUE_REPO", "")
	settings.Reset()
	if Catalogue(&http.Client{Transport: &fake{routes: routes}}) != nil {
		t.Error("switched off is nil")
	}
}

func TestParseCatalogueEntries(t *testing.T) {
	text := `
other = { "x" = {} }
repos = {
  "a" = {
    # "fake" = {
    required_status_checks = [
      { context = "pre-commit / Pre-commit" }, # trailing
      { context = "b" },
    ]
    nested = {
      "notarepo" = {
      }
    }
  }
  "empty" = {
  }
}
"after" = {
}
`
	got := ParseCatalogueEntries(text)
	if len(got) != 2 || len(got["a"].RequiredChecks) != 2 || !got["a"].RequiredChecks["pre-commit / Pre-commit"] || len(got["empty"].RequiredChecks) != 0 {
		t.Errorf("got %v", got)
	}
}
