package mailer

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"strings"
	"testing"

	"github.com/jay-withers/repo-agent/internal/settings"
)

type resend struct {
	status int
	sent   map[string]any
	auth   string
}

func (r *resend) RoundTrip(req *http.Request) (*http.Response, error) {
	r.auth = req.Header.Get("Authorization")
	_ = json.NewDecoder(req.Body).Decode(&r.sent)
	code := r.status
	if code == 0 {
		code = 200
	}
	return &http.Response{StatusCode: code, Header: http.Header{},
		Body: io.NopCloser(bytes.NewReader([]byte(`{"id": "re_1"}`)))}, nil
}

func isolate(t *testing.T, to string) {
	t.Helper()
	t.Chdir(t.TempDir())
	t.Setenv("KEY_VAULT_URI", "")
	t.Setenv("RESEND_API_KEY", "re_test")
	t.Setenv("DIGEST_EMAIL_TO", to)
	settings.Reset()
	t.Cleanup(settings.Reset)
}

func TestSkippedWithoutARecipient(t *testing.T) {
	isolate(t, "")
	r := &resend{}
	if got, _ := Send(&http.Client{Transport: r}, "s", "h", "t"); got.Status != "skipped" || r.sent != nil {
		t.Errorf("got %+v", got)
	}
}

func TestSendsToEveryRecipient(t *testing.T) {
	isolate(t, " a@example.com, ,b@example.com ")
	r := &resend{}
	got, _ := Send(&http.Client{Transport: r}, "subject", "<p>", "text")
	if got.Status != "sent" || got.ProviderID != "re_1" || r.auth != "Bearer re_test" {
		t.Errorf("got %+v, auth %q", got, r.auth)
	}
	to, _ := r.sent["to"].([]any)
	if len(to) != 2 || to[0] != "a@example.com" || r.sent["from"] != "repo-agent <onboarding@resend.dev>" {
		t.Errorf("sent %v", r.sent)
	}
}

func TestANonASCIIRecipientFailsWithoutNamingIt(t *testing.T) {
	isolate(t, "a@example.com,“b”@example.com")
	r := &resend{}
	got, _ := Send(&http.Client{Transport: r}, "s", "h", "t")
	if got.Status != "failed" || got.Error != "recipient is not plain ASCII: recipient 2 of 2 contains U+201C" || r.sent != nil {
		t.Errorf("got %+v", got)
	}
	if strings.Contains(got.Error, "example.com") {
		t.Error("the address must never be in the result")
	}
}

func TestAProviderFailureIsReportedNotRaised(t *testing.T) {
	isolate(t, "a@example.com")
	if got, err := Send(&http.Client{Transport: &resend{status: 422}}, "s", "h", "t"); err != nil || got.Status != "failed" || !strings.HasPrefix(got.Error, "422 from") {
		t.Errorf("got %+v", got)
	}
}

func TestMissingKeyOrBrokenVaultFails(t *testing.T) {
	isolate(t, "a@example.com")
	t.Setenv("RESEND_API_KEY", "")
	settings.Reset()
	r := &resend{}
	if _, err := Send(&http.Client{Transport: r}, "s", "h", "t"); err == nil || r.sent != nil {
		t.Errorf("a missing key is an error, and nothing is sent: %v", err)
	}

	t.Setenv("DIGEST_EMAIL_TO", "")
	t.Setenv("KEY_VAULT_URI", "https://kv.example")
	settings.Reset()
	restore := settings.VaultGetter
	t.Cleanup(func() { settings.VaultGetter = restore })
	settings.VaultGetter = func(context.Context, string, string) (string, bool, error) {
		return "", false, errors.New(strings.Repeat("e", 600))
	}
	if _, err := Send(&http.Client{Transport: &resend{}}, "s", "h", "t"); err == nil {
		t.Error("a broken vault is an error, not a skipped or failed send")
	}
}
