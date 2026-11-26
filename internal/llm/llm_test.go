package llm

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"
	"time"

	"github.com/jay-withers/repo-agent/internal/fetch"
	"github.com/jay-withers/repo-agent/internal/model"
	"github.com/jay-withers/repo-agent/internal/settings"
)

func isolate(t *testing.T, key string) {
	t.Helper()
	t.Chdir(t.TempDir())
	t.Setenv("KEY_VAULT_URI", "")
	t.Setenv("DEEPSEEK_API_KEY", key)
	settings.Reset()
	restore := fetch.Sleep
	fetch.Sleep = func(time.Duration) {}
	t.Cleanup(func() { settings.Reset(); fetch.Sleep = restore })
}

type deepseek struct {
	content  string
	finish   string
	status   int
	balance  any
	requests int
}

func (d *deepseek) RoundTrip(req *http.Request) (*http.Response, error) {
	d.requests++
	respond := func(code int, body any) (*http.Response, error) {
		data, _ := json.Marshal(body)
		return &http.Response{StatusCode: code, Header: http.Header{}, Body: io.NopCloser(bytes.NewReader(data))}, nil
	}
	if strings.HasSuffix(req.URL.Path, "/user/balance") {
		if d.balance == nil {
			return respond(500, map[string]any{})
		}
		return respond(200, d.balance)
	}
	if d.status != 0 {
		return respond(d.status, map[string]any{})
	}
	finish := d.finish
	if finish == "" {
		finish = "stop"
	}
	return respond(200, map[string]any{
		"choices": []any{map[string]any{"finish_reason": finish, "message": map[string]any{"content": d.content}}},
		"usage":   map[string]any{"prompt_tokens": 1000, "completion_tokens": 10},
	})
}

var findings = []model.Finding{
	{Repo: "o/a", Check: "one", Severity: "high", Title: "1"},
	{Repo: "o/a", Check: "two", Severity: "low", Title: "2"},
	{Repo: "o/b", Check: "three", Severity: "low", Title: "3"},
}

var repos = []model.RepoSnapshot{{FullName: "o/a"}, {FullName: "o/b"}}

func TestTriageIsOffWithoutAKey(t *testing.T) {
	isolate(t, "")
	ds := &deepseek{}
	out, _ := Triage(&http.Client{Transport: ds}, findings, repos)
	if len(out.Findings) != 3 || out.Usage != nil || ds.requests != 0 {
		t.Errorf("got %+v after %d requests", out, ds.requests)
	}
	if out, _ := Triage(&http.Client{Transport: ds}, nil, repos); len(out.Findings) != 0 || ds.requests != 0 {
		t.Error("nothing to triage asks nothing")
	}
}

func TestTriageReordersButNeverDrops(t *testing.T) {
	isolate(t, "sk")
	ds := &deepseek{content: `{"summary": " s ", "order": ["` + findings[2].ID() + `", "made-up"], "themes": ["t"],
		"suggestions": [{"repo": "o/zz", "text": "x"}, {"text": "estate"}, {"repo": "o/a", "text": " "}]}`,
		balance: map[string]any{"balance_infos": []any{map[string]any{"currency": "USD", "total_balance": json.Number("12.5")}}}}
	out, _ := Triage(&http.Client{Transport: ds}, findings, repos)
	if len(out.Findings) != 3 || out.Findings[0].Check != "three" || out.Findings[1].Check != "one" {
		t.Errorf("order %v", out.Findings)
	}
	if out.Summary != "s" || len(out.Suggestions) != 1 || out.Suggestions[0].Text != "estate" {
		t.Errorf("summary %q suggestions %v", out.Summary, out.Suggestions)
	}
	if out.Usage == nil || *out.Usage.BalanceUSD != "12.5" || out.Usage.CacheMissTokens != 1000 {
		t.Errorf("usage %+v", out.Usage)
	}
}

func TestTriageFailureKeepsTheFindings(t *testing.T) {
	cases := map[string]*deepseek{
		"truncated":   {content: "{", finish: "length"},
		"invalid":     {content: `{"summary": 3}`},
		"not json":    {content: "```json"},
		"server down": {status: 500},
	}
	for name, ds := range cases {
		isolate(t, "sk")
		out, _ := Triage(&http.Client{Transport: ds}, findings, repos)
		if len(out.Findings) != 3 || out.Summary != "" || out.Usage != nil {
			t.Errorf("%s: %+v", name, out)
		}
	}
}

func TestTriageWithAMissingBalanceStillReports(t *testing.T) {
	isolate(t, "sk")
	ds := &deepseek{content: `{}`}
	if out, _ := Triage(&http.Client{Transport: ds}, findings, repos); out.Usage == nil || out.Usage.BalanceUSD != nil {
		t.Errorf("usage %+v", out.Usage)
	}
	ds.balance = map[string]any{"balance_infos": []any{map[string]any{"currency": "CNY", "total_balance": "1"}}}
	if out, _ := Triage(&http.Client{Transport: ds}, findings, repos); out.Usage.BalanceUSD != nil {
		t.Error("no USD entry, no balance")
	}
}

func TestABrokenKeyFailsTheRun(t *testing.T) {
	isolate(t, "")
	t.Setenv("KEY_VAULT_URI", "https://kv.example")
	settings.Reset()
	restore := settings.VaultGetter
	t.Cleanup(func() { settings.VaultGetter = restore })
	settings.VaultGetter = func(context.Context, string, string) (string, bool, error) {
		return "", false, errors.New("401")
	}
	if _, err := Triage(&http.Client{Transport: &deepseek{}}, findings, repos); err == nil {
		t.Error("a credential that cannot read the key must not look like no key")
	}
}

func TestBalanceIsPythonsStr(t *testing.T) {
	cases := map[string]string{"5.10": "5.1", "5.0": "5.0", "5": "5", "1234567.0": "1234567.0", "0.00001": "1e-05",
		"1e16": "1e+16", "0.0001": "0.0001", "1e2": "100.0", "-0.5": "-0.5", "99999999999999999.0": "1e+17"}
	for in, want := range cases {
		if got := pyNumber(json.Number(in)); got != want {
			t.Errorf("%s: %s, want %s", in, got, want)
		}
	}
}

func TestParseReplyValidation(t *testing.T) {
	long := strings.Repeat("é", 2001)
	bad := []string{
		`[]`, `null`, `{"summary": null}`, `{"summary": "` + long + `"}`, `{"order": [1]}`, `{"order": null}`,
		`{"themes": ["a", "b", "c", "d"]}`, `{"themes": [null, "x"]}`, `{"order": ["a", null]}`, `{"suggestions": {}}`, `{"suggestions": [null]}`,
		`{"suggestions": [{"text": 1}]}`, `{"suggestions": [{"repo": "` + strings.Repeat("x", 201) + `"}]}`,
		`{"suggestions": [{"text": "` + strings.Repeat("x", 401) + `"}]}`,
	}
	for _, content := range bad {
		if _, err := ParseReply(content); err == nil {
			t.Errorf("%.60s should fail validation", content)
		}
	}
	r, err := ParseReply(`{"summary": "` + strings.Repeat("é", 2000) + `", "unknown": 1, "suggestions": [{"repo": "o/a", "text": "t"}]}`)
	if err != nil || r.Suggestions[0].Repo != "o/a" {
		t.Errorf("got %+v %v", r, err)
	}
}

func TestIsPeak(t *testing.T) {
	cases := map[string]bool{
		"2026-11-30T07:00:00Z": true,  // Monday, second window
		"2026-11-30T02:00:00Z": true,  // Monday, first window
		"2026-11-30T04:00:00Z": false, // between
		"2026-11-30T10:00:00Z": false, // window end is exclusive
		"2026-11-29T07:00:00Z": false, // Sunday
		"2026-11-28T07:00:00Z": false, // Saturday
	}
	for at, want := range cases {
		moment, _ := time.Parse(time.RFC3339, at)
		if IsPeak(moment) != want {
			t.Errorf("%s: want %v", at, want)
		}
	}
}

func TestUsageCostsPeakAtDouble(t *testing.T) {
	raw := map[string]any{"prompt_tokens": 1_000_000, "prompt_cache_hit_tokens": 400_000, "completion_tokens": 1_000_000}
	off := Usage(raw, "m", time.Date(2026, 11, 29, 7, 0, 0, 0, time.UTC))
	peak := Usage(raw, "m", time.Date(2026, 11, 30, 7, 0, 0, 0, time.UTC))
	if off.CacheMissTokens != 600_000 || *off.CostUSD < 0.6911 || *off.CostUSD > 0.6913 || !peak.Peak || *peak.CostUSD < 2**off.CostUSD-1e-9 {
		t.Errorf("off %+v (%v), peak %v", off, *off.CostUSD, *peak.CostUSD)
	}
}

func TestPromptClipsFilesAndSendsNoWorkflowText(t *testing.T) {
	readme := strings.Repeat("r", 1300)
	repo := model.RepoSnapshot{FullName: "o/a", Readme: &readme, RenovateConfig: model.Ptr(""),
		Workflows: []model.Workflow{{Name: "ci.yml", Text: "SECRET WORKFLOW TEXT"}}}
	prompt := UserPrompt(findings[:1], []model.RepoSnapshot{repo, {FullName: "o/clean"}})
	if strings.Contains(prompt, "SECRET WORKFLOW TEXT") || strings.Contains(prompt, "o/clean") ||
		!strings.Contains(prompt, `"workflows":["ci.yml"]`) || !strings.Contains(prompt, `"renovate_config":null`) ||
		!strings.Contains(prompt, strings.Repeat("r", 1200)+"\\n\\u2026 [truncated]") {
		t.Errorf("prompt: %s", prompt)
	}
	if strings.Contains(UserPrompt(findings, nil), `"repo":"o/a","description"`) {
		t.Error("a finding with no snapshot sends no context")
	}
}
