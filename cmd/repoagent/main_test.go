package main

import (
	"bytes"
	"context"
	"crypto/rand"
	"crypto/rsa"
	"crypto/x509"
	"encoding/json"
	"encoding/pem"
	"errors"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/jay-withers/repo-agent/internal/github"
	"github.com/jay-withers/repo-agent/internal/logging"
	"github.com/jay-withers/repo-agent/internal/model"
	"github.com/jay-withers/repo-agent/internal/settings"
	"github.com/jay-withers/repo-agent/internal/state"
)

type memStore struct {
	data    []byte
	fail    bool
	written []byte
}

func (m *memStore) Read(context.Context) ([]byte, bool, error) { return m.data, m.data != nil, nil }
func (m *memStore) Write(_ context.Context, d []byte) error {
	if m.fail {
		return errors.New("denied")
	}
	m.written = d
	return nil
}

func isolate(t *testing.T, store state.Store) {
	t.Helper()
	t.Chdir(t.TempDir())
	t.Setenv("KEY_VAULT_URI", "")
	settings.Reset()
	github.ResetCache()
	restore := state.DefaultStore
	state.DefaultStore = func() (state.Store, error) { return store, nil }
	logger := slog.Default()
	t.Cleanup(func() { settings.Reset(); github.ResetCache(); state.DefaultStore = restore; slog.SetDefault(logger) })
	model.Now = func() time.Time { return time.Date(2026, 11, 29, 18, 0, 0, 0, time.UTC) }
	t.Cleanup(func() { model.Now = func() time.Time { return time.Now().UTC() } })
}

func runCLI(argv ...string) (int, string, string) {
	var out, errs bytes.Buffer
	code := run(argv, &out, &errs)
	return code, out.String(), errs.String()
}

func TestUsageErrorsExitTwo(t *testing.T) {
	for _, argv := range [][]string{
		{}, {"bogus"}, {"suppress", "abc"}, {"suppress", "--reason"}, {"suppress", "--colour", "x"},
		{"unsuppress"}, {"scan", "extra"}, {"state", "-x"},
	} {
		if code, _, errs := runCLI(argv...); code != 2 || !strings.Contains(errs, "repoagent: error:") {
			t.Errorf("%v: exit %d, %q", argv, code, errs)
		}
	}
	if code, out, _ := runCLI("--help"); code != 0 || !strings.Contains(out, "usage: repoagent") {
		t.Error("--help")
	}
	if code, _, _ := runCLI("suppress", "-h"); code != 0 {
		t.Error("suppress -h")
	}
}

func TestParseAcceptsOptionsEitherSideOfTheID(t *testing.T) {
	for _, argv := range [][]string{
		{"suppress", "abc", "--reason", "why", "--until", "2026-12-31"},
		{"suppress", "--reason=why", "abc", "--until=2026-12-31"},
	} {
		a, err := parse(argv)
		if err != nil || a.findingID != "abc" || *a.reason != "why" || *a.until != "2026-12-31" {
			t.Errorf("%v: %+v %v", argv, a, err)
		}
	}
}

func TestStatePrintsTheDocument(t *testing.T) {
	isolate(t, &memStore{data: []byte(`{"findings": {"abc": {"first_seen": "2026-01-01"}}}`)})
	code, out, _ := runCLI("state")
	if code != 0 || !strings.Contains(out, `"abc": {`) || !strings.HasSuffix(out, "}\n") {
		t.Errorf("exit %d: %s", code, out)
	}
}

func TestSuppressAndUnsuppress(t *testing.T) {
	store := &memStore{data: []byte(`{"findings": {"abc": {"first_seen": "2026-01-01"}}}`)}
	isolate(t, store)
	code, out, _ := runCLI("suppress", "abc", "--reason", "known", "--until", "2027-01-01")
	if code != 0 || !strings.Contains(out, "INFO    repoagent.state state updated: abc suppressd") {
		t.Errorf("exit %d: %s", code, out)
	}
	saved, _ := state.FromJSON(store.written)
	rule := saved.Suppressed["abc"]
	if rule.Reason != "known" || *rule.Until != "2027-01-01" || rule.Added != "2026-11-29" {
		t.Errorf("rule %+v", rule)
	}

	store.data = store.written
	if code, _, _ := runCLI("unsuppress", "abc"); code != 0 {
		t.Error("unsuppress")
	}
	if saved, _ := state.FromJSON(store.written); len(saved.Suppressed) != 0 {
		t.Error("still suppressed")
	}
}

func TestOperatorCommandsReportFailure(t *testing.T) {
	store := &memStore{data: []byte(`{}`)}
	isolate(t, store)
	if code, out, _ := runCLI("unsuppress", "nope"); code != 1 || !strings.Contains(out, "ERROR   repoagent.state nope is not suppressed") {
		t.Errorf("exit %d: %s", code, out)
	}
	if code, out, _ := runCLI("suppress", "x", "--reason", "r", "--until", "soon"); code != 1 || !strings.Contains(out, "is not a YYYY-MM-DD date") {
		t.Errorf("exit %d: %s", code, out)
	}
	code, out, _ := runCLI("suppress", "unknown", "--reason", "pre-emptive")
	if code != 0 || !strings.Contains(out, "WARNING repoagent.state unknown is not a finding this run knows about") {
		t.Errorf("an unknown id warns and still suppresses: exit %d: %s", code, out)
	}
	store.fail = true
	if code, out, _ := runCLI("suppress", "x", "--reason", "r"); code != 1 || !strings.Contains(out, "nothing was changed") {
		t.Errorf("a failed save is a failure: exit %d: %s", code, out)
	}
}

func testKey(t *testing.T) string {
	key, err := rsa.GenerateKey(rand.Reader, 2048)
	if err != nil {
		t.Fatal(err)
	}
	der, _ := x509.MarshalPKCS8PrivateKey(key)
	return string(pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: der}))
}

type github200 struct{}

func (github200) RoundTrip(req *http.Request) (*http.Response, error) {
	body := any([]any{map[string]any{"id": 1}})
	if req.Method == http.MethodPost {
		body = map[string]any{"token": "ghs", "expires_at": "2099-01-01T00:00:00Z"}
	}
	data, _ := json.Marshal(body)
	return &http.Response{StatusCode: 200, Header: http.Header{}, Body: io.NopCloser(bytes.NewReader(data))}, nil
}

func TestSelfcheck(t *testing.T) {
	isolate(t, nil)
	var out bytes.Buffer
	slog.SetDefault(slog.New(logging.NewHandler(&out, "INFO", "repoagent")))
	if code := selfcheck(); code != 1 || !strings.Contains(out.String(), "selfcheck: FAIL secret GITHUB-APP-ID") ||
		!strings.Contains(out.String(), "selfcheck: ok secret DIGEST-EMAIL-TO (optional)") {
		t.Errorf("exit %d: %s", code, out.String())
	}

	t.Setenv("GITHUB_APP_ID", "1")
	t.Setenv("GITHUB_APP_PRIVATE_KEY", testKey(t))
	t.Setenv("RESEND_API_KEY", "re")
	settings.Reset()
	restore := http.DefaultTransport
	http.DefaultTransport = github200{}
	t.Cleanup(func() { http.DefaultTransport = restore })
	out.Reset()
	if code := selfcheck(); code != 0 || !strings.Contains(out.String(), "selfcheck: all checks passed") ||
		strings.Contains(out.String(), "ghs") {
		t.Errorf("exit %d: %s", code, out.String())
	}
}

func TestScanFailureExitsOne(t *testing.T) {
	isolate(t, nil)
	if code, out, _ := runCLI("render"); code != 1 || !strings.Contains(out, "ERROR   repoagent.cli repoagent render failed:") {
		t.Errorf("no credentials: exit %d: %s", code, out)
	}
}
