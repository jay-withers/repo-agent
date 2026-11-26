package scan

import (
	"context"
	"errors"
	"net/http"
	"testing"

	"github.com/jay-withers/repo-agent/internal/settings"
)

// TestASecretFailureFailsTheRunAndSavesNothing: when the recipient cannot be
// read, no email was attempted, so the findings must not be recorded as
// seen, and the job must fail rather than exit 0.
func TestASecretFailureFailsTheRunAndSavesNothing(t *testing.T) {
	fx := loadFixture(t, "full")
	store := setupScenario(t, fx)
	t.Setenv("KEY_VAULT_URI", "https://kv.example")
	t.Setenv("DIGEST_EMAIL_TO", "")
	settings.Reset()
	restore := settings.VaultGetter
	t.Cleanup(func() { settings.VaultGetter = restore })
	settings.VaultGetter = func(_ context.Context, _, name string) (string, bool, error) {
		return "", false, errors.New("403 reading " + name)
	}
	_, err := Run(&http.Client{Transport: transport{t: t, fx: fx, got: &captured{}}}, true)
	if err == nil {
		t.Error("the run must fail")
	}
	if store.saved != nil {
		t.Error("state was saved for a digest nobody was sent")
	}
}
