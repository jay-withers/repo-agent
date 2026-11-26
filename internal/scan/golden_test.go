package scan

// The parity test: the Go scan, replayed against the fixture the Python scan
// rendered testdata/golden from (scripts/golden.py, `make golden`), must
// produce the same digest, the same DeepSeek prompt, the same GraphQL query
// and the same state document, byte for byte.

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"fmt"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/jay-withers/repo-agent/internal/digest"
	"github.com/jay-withers/repo-agent/internal/fetch"
	"github.com/jay-withers/repo-agent/internal/github"
	"github.com/jay-withers/repo-agent/internal/model"
	"github.com/jay-withers/repo-agent/internal/settings"
	"github.com/jay-withers/repo-agent/internal/state"
)

// goldenDir is absolute: each case chdirs into an empty directory.
var goldenDir = func() string {
	dir, err := filepath.Abs("../../testdata/golden")
	if err != nil {
		panic(err)
	}
	return dir
}()

type fixture struct {
	Now           string                     `json:"now"`
	SendEmail     bool                       `json:"send_email"`
	ImageTag      *string                    `json:"image_tag"`
	Permissions   map[string]string          `json:"permissions"`
	Repos         []json.RawMessage          `json:"repos"`
	Details       map[string]json.RawMessage `json:"details"`
	Alerts        map[string]json.RawMessage `json:"alerts"`
	AlertsMissing []string                   `json:"alerts_missing"`
	Issues        map[string]json.RawMessage `json:"issues"`
	Catalogue     *string                    `json:"catalogue"`
	DetailError   string                     `json:"detail_error"`
	PriorState    json.RawMessage            `json:"prior_state"`
	EmailTo       *string                    `json:"email_to"`
	DeepSeek      *struct {
		Reply   json.RawMessage `json:"reply"`
		Model   string          `json:"model"`
		Usage   json.RawMessage `json:"usage"`
		Balance json.RawMessage `json:"balance"`
	} `json:"deepseek"`
}

var (
	keyOnce sync.Once
	keyPEM  string
)

// testKey is an RSA key generated in process: never a committed PEM, even
// a throwaway, in a public repository.
func testKey(t *testing.T) string {
	keyOnce.Do(func() {
		key, err := rsa.GenerateKey(rand.Reader, 2048)
		if err != nil {
			t.Fatal(err)
		}
		der, err := x509.MarshalPKCS8PrivateKey(key)
		if err != nil {
			t.Fatal(err)
		}
		keyPEM = string(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}))
	})
	return keyPEM
}

type memStore struct {
	prior []byte
	saved []byte
}

func (m *memStore) Read(context.Context) ([]byte, bool, error) {
	return m.prior, m.prior != nil, nil
}

func (m *memStore) Write(_ context.Context, data []byte) error {
	m.saved = data
	return nil
}

type captured struct {
	detailQuery  string
	prompt       string
	systemPrompt string
	email        map[string]any
}

type transport struct {
	t   *testing.T
	fx  fixture
	got *captured
}

func reply(status int, body any) *http.Response {
	var data []byte
	switch b := body.(type) {
	case json.RawMessage:
		data = b
	default:
		data, _ = json.Marshal(b)
	}
	return &http.Response{StatusCode: status, Header: http.Header{"Content-Type": {"application/json"}},
		Body: io.NopCloser(bytes.NewReader(data))}
}

func (tr transport) RoundTrip(req *http.Request) (*http.Response, error) {
	var body map[string]any
	if req.Body != nil {
		data, _ := io.ReadAll(req.Body)
		_ = json.Unmarshal(data, &body)
	}
	path := req.URL.Path
	fx := tr.fx
	switch req.URL.Host {
	case "api.github.com":
		switch {
		case path == "/app/installations":
			return reply(200, []any{map[string]any{"id": 42, "account": map[string]any{"login": "jay-withers"}}}), nil
		case path == "/app/installations/42/access_tokens":
			return reply(201, map[string]any{"token": "ghs_test", "expires_at": "2099-01-01T00:00:00Z", "permissions": fx.Permissions}), nil
		case path == "/installation/repositories":
			return reply(200, map[string]any{"total_count": len(fx.Repos), "repositories": fx.Repos}), nil
		case strings.HasSuffix(path, "/code-scanning/alerts"):
			name := strings.TrimSuffix(strings.TrimPrefix(path, "/repos/"), "/code-scanning/alerts")
			page, ok := fx.Alerts[name]
			for _, missing := range fx.AlertsMissing {
				ok = ok && missing != name
			}
			if !ok {
				return reply(404, map[string]any{"message": "no analysis found"}), nil
			}
			return reply(200, page), nil
		case strings.HasSuffix(path, "/issues"):
			name := strings.TrimSuffix(strings.TrimPrefix(path, "/repos/"), "/issues")
			if page, ok := fx.Issues[name]; ok {
				return reply(200, page), nil
			}
			return reply(200, []any{}), nil
		case path == "/graphql":
			query, _ := body["query"].(string)
			if strings.Contains(query, "query Catalogue") {
				var obj any
				if fx.Catalogue != nil {
					obj = map[string]any{"text": *fx.Catalogue}
				}
				return reply(200, map[string]any{"data": map[string]any{"repository": map[string]any{"object": obj}}}), nil
			}
			if tr.got.detailQuery == "" {
				tr.got.detailQuery = query
			}
			if fx.DetailError != "" {
				return reply(200, map[string]any{"errors": []any{map[string]any{"message": fx.DetailError}}}), nil
			}
			variables, _ := body["variables"].(map[string]any)
			data := map[string]any{}
			for key, owner := range variables {
				if !strings.HasPrefix(key, "o") {
					continue
				}
				index := key[1:]
				full := fmt.Sprintf("%s/%s", owner, variables["n"+index])
				if d, ok := fx.Details[full]; ok {
					data["r"+index] = d
				} else {
					data["r"+index] = nil
				}
			}
			return reply(200, map[string]any{"data": data}), nil
		}
	case "api.deepseek.com":
		switch path {
		case "/chat/completions":
			messages, _ := body["messages"].([]any)
			tr.got.systemPrompt, _ = messages[0].(map[string]any)["content"].(string)
			tr.got.prompt, _ = messages[1].(map[string]any)["content"].(string)
			return reply(200, map[string]any{
				"model":   fx.DeepSeek.Model,
				"choices": []any{map[string]any{"finish_reason": "stop", "message": map[string]any{"content": string(fx.DeepSeek.Reply)}}},
				"usage":   fx.DeepSeek.Usage,
			}), nil
		case "/user/balance":
			return reply(200, fx.DeepSeek.Balance), nil
		}
	case "api.resend.com":
		tr.got.email = body
		return reply(200, map[string]any{"id": "re_123"}), nil
	}
	tr.t.Errorf("unexpected request %s %s", req.Method, req.URL)
	return reply(500, map[string]any{}), nil
}

func compare(t *testing.T, scenario, name, got string) {
	t.Helper()
	want, err := os.ReadFile(filepath.Join(goldenDir, scenario, name))
	if err != nil {
		t.Fatalf("%s/%s: %v (run `make golden`)", scenario, name, err)
	}
	if got == string(want) {
		return
	}
	gl, wl := strings.Split(got, "\n"), strings.Split(string(want), "\n")
	for i := 0; i < max(len(gl), len(wl)); i++ {
		var g, w string
		if i < len(gl) {
			g = gl[i]
		}
		if i < len(wl) {
			w = wl[i]
		}
		if g != w {
			t.Errorf("%s/%s differs from the Python at line %d:\n  go:     %q\n  python: %q", scenario, name, i+1, g, w)
			return
		}
	}
	t.Errorf("%s/%s differs from the Python", scenario, name)
}

func TestGoldenParity(t *testing.T) {
	entries, err := os.ReadDir(goldenDir)
	if err != nil {
		t.Fatal(err)
	}
	for _, entry := range entries {
		if !entry.IsDir() {
			continue
		}
		scenario := entry.Name()
		t.Run(scenario, func(t *testing.T) { runGolden(t, scenario) })
	}
}

func loadFixture(t *testing.T, scenario string) fixture {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join(goldenDir, scenario, "fixture.json"))
	if err != nil {
		t.Fatal(err)
	}
	var fx fixture
	if err := json.Unmarshal(raw, &fx); err != nil {
		t.Fatal(err)
	}
	return fx
}

// setupScenario puts the process where the Python harness put it: an empty
// directory, the fixture's secrets and clock, no sleeping, and the fixture's
// prior state in a store that records what is saved.
func setupScenario(t *testing.T, fx fixture) *memStore {
	t.Helper()
	now, err := time.Parse(time.RFC3339, fx.Now)
	if err != nil {
		t.Fatal(err)
	}
	t.Chdir(t.TempDir())
	t.Setenv("GITHUB_APP_ID", "123456")
	t.Setenv("GITHUB_APP_PRIVATE_KEY", testKey(t))
	t.Setenv("RESEND_API_KEY", "re_test")
	t.Setenv("KEY_VAULT_URI", "")
	for _, name := range []string{"DIGEST_EMAIL_TO", "DEEPSEEK_API_KEY", "IMAGE_TAG", "STATE_CONTAINER_URL"} {
		t.Setenv(name, "")
		_ = os.Unsetenv(name)
	}
	if fx.EmailTo != nil {
		t.Setenv("DIGEST_EMAIL_TO", *fx.EmailTo)
	}
	if fx.DeepSeek != nil {
		t.Setenv("DEEPSEEK_API_KEY", "sk-test")
	}
	if fx.ImageTag != nil {
		t.Setenv("IMAGE_TAG", *fx.ImageTag)
	}
	settings.Reset()
	github.ResetCache()
	t.Cleanup(func() { settings.Reset(); github.ResetCache() })

	restoreNow, restoreSleep, restoreStore := model.Now, fetch.Sleep, state.DefaultStore
	t.Cleanup(func() { model.Now, fetch.Sleep, state.DefaultStore = restoreNow, restoreSleep, restoreStore })
	model.Now = func() time.Time { return now.UTC() }
	fetch.Sleep = func(time.Duration) {}
	store := &memStore{}
	if len(fx.PriorState) > 0 && string(fx.PriorState) != "null" {
		store.prior = fx.PriorState
		state.DefaultStore = func() (state.Store, error) { return store, nil }
	} else {
		state.DefaultStore = func() (state.Store, error) { return nil, nil }
	}
	return store
}

func runGolden(t *testing.T, scenario string) {
	fx := loadFixture(t, scenario)
	store := setupScenario(t, fx)

	got := &captured{}
	client := &http.Client{Transport: transport{t: t, fx: fx, got: got}}
	result, err := Run(client, fx.SendEmail)
	if err != nil {
		t.Fatal(err)
	}

	subject, text, html := digest.Subject(result), digest.Text(result), digest.HTML(result)
	compare(t, scenario, "subject.txt", subject)
	compare(t, scenario, "text.txt", text)
	compare(t, scenario, "html.html", html)
	if got.email != nil {
		if got.email["subject"] != subject || got.email["text"] != text || got.email["html"] != html {
			t.Error("the email sent is not the digest rendered")
		}
	}
	expect := func(name, value string) {
		_, statErr := os.Stat(filepath.Join(goldenDir, scenario, name))
		switch {
		case statErr == nil:
			compare(t, scenario, name, value)
		case value != "":
			t.Errorf("%s/%s: the Go produced one and the Python did not", scenario, name)
		}
	}
	expect("prompt.json", got.prompt)
	expect("system_prompt.txt", got.systemPrompt)
	expect("detail_query.graphql", got.detailQuery)
	expect("state.json", string(store.saved))
}
