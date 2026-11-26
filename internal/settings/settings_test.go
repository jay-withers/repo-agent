package settings

import (
	"context"
	"errors"
	"os"
	"strings"
	"testing"

	"github.com/Azure/azure-sdk-for-go/sdk/azcore/policy"
)

// isolate runs a test in an empty directory with no vault and fresh caches,
// as the Python conftest did: a developer's .env must not reach the suite.
func isolate(t *testing.T) {
	t.Helper()
	t.Chdir(t.TempDir())
	t.Setenv("KEY_VAULT_URI", "")
	Reset()
	t.Cleanup(Reset)
	restore := VaultGetter
	t.Cleanup(func() { VaultGetter = restore })
	VaultGetter = func(context.Context, string, string) (string, bool, error) {
		t.Fatal("a test reached for Key Vault")
		return "", false, nil
	}
}

func writeDotenv(t *testing.T, text string) {
	t.Helper()
	if err := os.WriteFile(".env", []byte(text), 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestDefaults(t *testing.T) {
	isolate(t)
	s := Get()
	if s.DeepSeekModel != "deepseek-flash" || s.GitHubAPIURL != "https://api.github.com" || s.CatalogueRepo != "jay-withers/github-repos" {
		t.Errorf("defaults: %+v", s)
	}
	if !s.IgnoredTopics()["no-scan"] || !s.IgnoredTopics()["tutorial"] || len(s.IgnoredTopics()) != 2 {
		t.Errorf("ignored topics: %v", s.IgnoredTopics())
	}
}

func TestEnvironmentBeatsDotenvAndEmptyStillCounts(t *testing.T) {
	isolate(t)
	writeDotenv(t, "STATE_CONTAINER_URL=https://from.dotenv\nDEEPSEEK_MODEL=from-dotenv\nIGNORE_TOPICS= A, ,b \n")
	t.Setenv("STATE_CONTAINER_URL", "")
	s := Get()
	if s.StateContainerURL != "" {
		t.Errorf("an explicit empty variable must beat .env, got %q", s.StateContainerURL)
	}
	if s.DeepSeekModel != "from-dotenv" {
		t.Errorf(".env beats the default, got %q", s.DeepSeekModel)
	}
	topics := s.IgnoredTopics()
	if !topics["a"] || !topics["b"] || len(topics) != 2 {
		t.Errorf("topics trimmed, lowered, blanks dropped: %v", topics)
	}
}

func TestParseDotenv(t *testing.T) {
	isolate(t)
	writeDotenv(t, strings.Join([]string{
		"# a comment",
		"",
		"PLAIN=value # trailing comment",
		"export EXPORTED=yes",
		`DOUBLE="line one\nline \"two\""`,
		"SINGLE='kept # as is'",
		"NOEQUALS",
		" SPACED = padded ",
	}, "\n"))
	got := ParseDotenv(".env")
	want := map[string]string{
		"PLAIN":    "value",
		"EXPORTED": "yes",
		"DOUBLE":   "line one\nline \"two\"",
		"SINGLE":   "kept # as is",
		"SPACED":   "padded",
	}
	if len(got) != len(want) {
		t.Errorf("got %q", got)
	}
	for k, v := range want {
		if got[k] != v {
			t.Errorf("%s = %q, want %q", k, got[k], v)
		}
	}
	if len(ParseDotenv("missing")) != 0 {
		t.Error("a missing file is empty")
	}
}

func TestSecretReadsEnvironmentThenDotenv(t *testing.T) {
	isolate(t)
	writeDotenv(t, "RESEND_API_KEY=from-dotenv\nGITHUB_APP_ID=dotenv-id\n")
	t.Setenv("GITHUB_APP_ID", "env-id")
	t.Setenv("RESEND_API_KEY", "")
	if v, err := Secret("GITHUB-APP-ID"); err != nil || v != "env-id" {
		t.Errorf("environment first: %q %v", v, err)
	}
	// Unlike settings, an empty variable falls through (appkit's `or`).
	if v, err := Secret("RESEND-API-KEY"); err != nil || v != "from-dotenv" {
		t.Errorf("then .env: %q %v", v, err)
	}
}

func TestSecretWithoutVaultSaysHowToSetIt(t *testing.T) {
	isolate(t)
	_, err := Secret("GITHUB-APP-PRIVATE-KEY")
	if err == nil || !strings.Contains(err.Error(), "$GITHUB_APP_PRIVATE_KEY") ||
		!strings.Contains(err.Error(), "az keyvault secret set --name GITHUB-APP-PRIVATE-KEY") {
		t.Errorf("got %v", err)
	}
	if v, err := OptionalSecret("DIGEST-EMAIL-TO"); v != "" || err != nil {
		t.Errorf("optional with no vault is absent: %q %v", v, err)
	}
}

func TestARequiredSecretMissingFromTheVaultIsAnError(t *testing.T) {
	isolate(t)
	t.Setenv("KEY_VAULT_URI", "https://kv.example")
	Reset()
	VaultGetter = func(context.Context, string, string) (string, bool, error) { return "", false, nil }
	if v, err := Secret("RESEND-API-KEY"); err == nil || v != "" || !strings.Contains(err.Error(), "not in Key Vault") {
		t.Errorf("got %q %v", v, err)
	}
	if v, ok := cachedSecret("RESEND-API-KEY"); ok {
		t.Errorf("an absent secret is not cached: %q", v)
	}
}

func TestMultiLineQuotedValues(t *testing.T) {
	isolate(t)
	writeDotenv(t, "KEY=\"-----BEGIN-----\nabc\ndef\n-----END-----\"\nAFTER=1\n"+
		"SINGLE='one\ntwo'\nESCAPED=\"say \\\"hi\\\"\"\nOPEN=\"never closed\n")
	got := ParseDotenv(".env")
	if got["KEY"] != "-----BEGIN-----\nabc\ndef\n-----END-----" || got["AFTER"] != "1" ||
		got["SINGLE"] != "one\ntwo" || got["ESCAPED"] != `say "hi"` || got["OPEN"] != "never closed\n" {
		t.Errorf("got %q", got)
	}
}

func TestSecretFromVaultIsCached(t *testing.T) {
	isolate(t)
	t.Setenv("KEY_VAULT_URI", "https://kv.example")
	Reset()
	calls := 0
	VaultGetter = func(_ context.Context, uri, name string) (string, bool, error) {
		calls++
		if uri != "https://kv.example" || name != "RESEND-API-KEY" {
			t.Errorf("asked %s for %s", uri, name)
		}
		return "re_vault", true, nil
	}
	for range 2 {
		if v, err := Secret("RESEND-API-KEY"); v != "re_vault" || err != nil {
			t.Errorf("got %q %v", v, err)
		}
	}
	if calls != 1 {
		t.Errorf("the vault was asked %d times", calls)
	}
}

func TestOptionalSecretAbsentIsEmptyButBrokenIsAnError(t *testing.T) {
	isolate(t)
	t.Setenv("KEY_VAULT_URI", "https://kv.example")
	Reset()
	VaultGetter = func(_ context.Context, _, name string) (string, bool, error) {
		if name == "DIGEST-EMAIL-TO" {
			return "", false, nil
		}
		return "", false, errors.New("401 from the vault")
	}
	if v, err := OptionalSecret("DIGEST-EMAIL-TO"); v != "" || err != nil {
		t.Errorf("absent: %q %v", v, err)
	}
	if _, err := OptionalSecret("DEEPSEEK-API-KEY"); err == nil {
		t.Error("a broken credential must not look like not configured")
	}
	if _, err := Secret("RESEND-API-KEY"); err == nil {
		t.Error("required and broken is an error")
	}
}

func TestCredentialPrefersAStaticToken(t *testing.T) {
	isolate(t)
	t.Setenv("AZURE_KEYVAULT_TOKEN", "tok")
	Reset()
	cred, err := Credential()
	if err != nil {
		t.Fatal(err)
	}
	tok, err := cred.GetToken(context.Background(), policy.TokenRequestOptions{Scopes: []string{KeyVaultScope}})
	if err != nil || tok.Token != "tok" {
		t.Errorf("got %v %v", tok, err)
	}
	if _, err := cred.GetToken(context.Background(), policy.TokenRequestOptions{Scopes: []string{"https://storage.azure.com/.default"}}); err == nil {
		t.Error("a Key Vault token must refuse another scope")
	}
	if _, err := cred.GetToken(context.Background(), policy.TokenRequestOptions{}); err != nil {
		t.Errorf("no scope asked: %v", err)
	}
}

func TestCredentialFallsBackToManagedIdentityThenDefault(t *testing.T) {
	isolate(t)
	t.Setenv("AZURE_CLIENT_ID", "00000000-0000-0000-0000-000000000000")
	Reset()
	if cred, err := Credential(); err != nil || cred == nil {
		t.Errorf("managed identity: %v %v", cred, err)
	}
	t.Setenv("AZURE_CLIENT_ID", "")
	Reset()
	if cred, err := Credential(); err != nil || cred == nil {
		t.Errorf("default: %v %v", cred, err)
	}
}
