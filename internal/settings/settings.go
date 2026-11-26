// Package settings is configuration and secret resolution.
//
// Settings are read from the environment, then `.env` in the working
// directory, then the defaults below: the order pydantic-settings used. A
// variable that is set but empty still wins over `.env`, which is what lets a
// test pin KEY_VAULT_URI to "" and be sure no vault is consulted.
//
// Secrets follow appkit's rule (appkit/keyvault.py): `Secret("RESEND-API-KEY")`
// reads $RESEND_API_KEY, then `.env`, then the Key Vault secret
// `RESEND-API-KEY`. Unlike settings, an empty variable falls through to the
// next source, as appkit's `os.environ.get(...) or dotenv().get(...)` does.
// appkit is Python-only, so this is a port of its behaviour and its tests;
// it moves to a shared Go module once a second Go app needs it.
package settings

import (
	"context"
	"errors"
	"fmt"
	"net/http"
	"os"
	"strings"
	"sync"
	"time"

	"github.com/Azure/azure-sdk-for-go/sdk/azcore"
	"github.com/Azure/azure-sdk-for-go/sdk/azcore/policy"
	"github.com/Azure/azure-sdk-for-go/sdk/azidentity"
	"github.com/Azure/azure-sdk-for-go/sdk/security/keyvault/azsecrets"
)

// KeyVaultScope is the scope a Key Vault data-plane token is issued for.
const KeyVaultScope = "https://vault.azure.net/.default"

// Settings is non-secret configuration. Anything here is safe in a log line.
type Settings struct {
	Environment        string
	KeyVaultURI        string
	AzureClientID      string
	AzureKeyVaultToken string
	GitHubAPIURL       string
	GitHubGraphQLURL   string
	DeepSeekAPIURL     string
	// deepseek-flash, named explicitly: `deepseek-chat` is an undocumented
	// alias that resolves to it and can be withdrawn without notice.
	DeepSeekModel string
	// Comma-separated topics whose repositories are skipped entirely.
	IgnoreTopics  string
	CatalogueRepo string
	CataloguePath string
	// Blob container holding the scan's history. Empty switches history off.
	StateContainerURL string
	DigestEmailFrom   string
	LogLevel          string
}

// IgnoredTopics is IgnoreTopics as a set, blanks and casing handled.
func (s Settings) IgnoredTopics() map[string]bool {
	topics := map[string]bool{}
	for _, t := range strings.Split(s.IgnoreTopics, ",") {
		if t = strings.TrimSpace(t); t != "" {
			topics[strings.ToLower(t)] = true
		}
	}
	return topics
}

var (
	mu       sync.Mutex
	cached   *Settings
	dotenv   map[string]string
	secrets  = map[string]*string{}
	cred     azcore.TokenCredential
	credErr  error
	credDone bool
)

// Reset drops every cache: settings, `.env`, resolved secrets and the
// credential. For tests, which change the environment between cases.
func Reset() {
	mu.Lock()
	defer mu.Unlock()
	cached, dotenv, credDone, cred, credErr = nil, nil, false, nil, nil
	secrets = map[string]*string{}
}

// Get returns the settings, read once.
func Get() Settings {
	mu.Lock()
	defer mu.Unlock()
	if cached == nil {
		env := dotenvLocked()
		get := func(name, fallback string) string {
			if v, ok := os.LookupEnv(name); ok {
				return v
			}
			if v, ok := env[name]; ok {
				return v
			}
			return fallback
		}
		cached = &Settings{
			Environment:        get("ENVIRONMENT", "dev"),
			KeyVaultURI:        get("KEY_VAULT_URI", ""),
			AzureClientID:      get("AZURE_CLIENT_ID", ""),
			AzureKeyVaultToken: get("AZURE_KEYVAULT_TOKEN", ""),
			GitHubAPIURL:       get("GITHUB_API_URL", "https://api.github.com"),
			GitHubGraphQLURL:   get("GITHUB_GRAPHQL_URL", "https://api.github.com/graphql"),
			DeepSeekAPIURL:     get("DEEPSEEK_API_URL", "https://api.deepseek.com"),
			DeepSeekModel:      get("DEEPSEEK_MODEL", "deepseek-flash"),
			IgnoreTopics:       get("IGNORE_TOPICS", "no-scan,tutorial"),
			CatalogueRepo:      get("CATALOGUE_REPO", "jay-withers/github-repos"),
			CataloguePath:      get("CATALOGUE_PATH", "terraform/terraform.tfvars"),
			StateContainerURL:  get("STATE_CONTAINER_URL", ""),
			DigestEmailFrom:    get("DIGEST_EMAIL_FROM", "repo-agent <onboarding@resend.dev>"),
			LogLevel:           get("LOG_LEVEL", "INFO"),
		}
	}
	return *cached
}

func dotenvLocked() map[string]string {
	if dotenv == nil {
		dotenv = ParseDotenv(".env")
	}
	return dotenv
}

// ParseDotenv reads a `.env` file as python-dotenv does for the forms this
// estate writes: KEY=value, optional `export `, `#` comments outside quotes,
// and single- or double-quoted values, which may span lines (the App's
// private key does). Double quotes take \n, \" and \\ escapes. A missing
// file is an empty map.
func ParseDotenv(path string) map[string]string {
	values := map[string]string{}
	raw, err := os.ReadFile(path) //nolint:gosec // the working directory's own .env, by design
	if err != nil {
		return values
	}
	lines := strings.Split(strings.ReplaceAll(string(raw), "\r\n", "\n"), "\n")
	for i := 0; i < len(lines); i++ {
		line := strings.TrimSpace(lines[i])
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		line = strings.TrimPrefix(line, "export ")
		key, value, ok := strings.Cut(line, "=")
		if !ok {
			continue
		}
		key = strings.TrimSpace(key)
		value = strings.TrimLeft(value, " \t")
		if value != "" && (value[0] == '"' || value[0] == '\'') {
			quote := value[0]
			body := value[1:]
			// Read on until the closing quote, across lines if need be.
			for closing(body, quote) < 0 && i+1 < len(lines) {
				i++
				body += "\n" + lines[i]
			}
			if end := closing(body, quote); end >= 0 {
				body = body[:end]
			}
			if quote == '"' {
				body = strings.NewReplacer(`\n`, "\n", `\"`, `"`, `\\`, `\`).Replace(body)
			}
			values[key] = body
			continue
		}
		if j := strings.Index(value, " #"); j >= 0 {
			value = value[:j]
		}
		values[key] = strings.TrimSpace(value)
	}
	return values
}

// closing is the index of the unescaped closing quote in s, or -1.
func closing(s string, quote byte) int {
	for i := 0; i < len(s); i++ {
		switch {
		case s[i] == '\\' && quote == '"':
			i++
		case s[i] == quote:
			return i
		}
	}
	return -1
}

func envName(name string) string {
	return strings.ToUpper(strings.ReplaceAll(name, "-", "_"))
}

// FromEnvironment is a secret from the process environment, then `.env`.
// Empty counts as absent in both.
func FromEnvironment(name string) string {
	variable := envName(name)
	if v := os.Getenv(variable); v != "" {
		return v
	}
	mu.Lock()
	defer mu.Unlock()
	return dotenvLocked()[variable]
}

// VaultGetter reads one secret from Key Vault. found is false for a secret
// the vault does not hold; err is anything else (auth, network). Swappable
// so tests never reach Azure.
var VaultGetter = func(ctx context.Context, vaultURL, name string) (value string, found bool, err error) {
	credential, err := Credential()
	if err != nil {
		return "", false, err
	}
	client, err := azsecrets.NewClient(vaultURL, credential, nil)
	if err != nil {
		return "", false, err
	}
	resp, err := client.GetSecret(ctx, name, "", nil)
	if err != nil {
		var re *azcore.ResponseError
		if errors.As(err, &re) && re.StatusCode == http.StatusNotFound {
			return "", false, nil
		}
		return "", false, err
	}
	if resp.Value == nil {
		return "", true, nil
	}
	return *resp.Value, true, nil
}

// Secret resolves a required secret by its hyphenated Key Vault name, and
// fails, saying how to set it, when nothing holds it.
func Secret(name string) (string, error) {
	if v := FromEnvironment(name); v != "" {
		return v, nil
	}
	uri := Get().KeyVaultURI
	if uri == "" {
		return "", fmt.Errorf(
			"%s is not set and no KEY_VAULT_URI is configured. Set $%s locally, or "+
				"`az keyvault secret set --name %s` for a deployed environment",
			name, envName(name), name)
	}
	if v, ok := cachedSecret(name); ok {
		return v, nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	value, found, err := VaultGetter(ctx, uri, name)
	if err != nil {
		return "", err
	}
	if !found {
		// appkit's resolve lets the vault's ResourceNotFoundError through.
		return "", fmt.Errorf("%s is not in Key Vault %s. `az keyvault secret set --name %s` sets it", name, uri, name)
	}
	storeSecret(name, value)
	return value, nil
}

// OptionalSecret resolves a secret that is allowed not to exist: "" when it
// is in no source. An authentication or network error is still an error,
// because "the credential is broken" must not look like "not configured".
func OptionalSecret(name string) (string, error) {
	if v := FromEnvironment(name); v != "" {
		return v, nil
	}
	uri := Get().KeyVaultURI
	if uri == "" {
		return "", nil
	}
	if v, ok := cachedSecret(name); ok {
		return v, nil
	}
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	value, found, err := VaultGetter(ctx, uri, name)
	if err != nil {
		return "", err
	}
	if found {
		storeSecret(name, value)
	}
	return value, nil
}

func cachedSecret(name string) (string, bool) {
	mu.Lock()
	defer mu.Unlock()
	v, ok := secrets[name]
	if !ok {
		return "", false
	}
	return *v, true
}

func storeSecret(name, value string) {
	mu.Lock()
	defer mu.Unlock()
	secrets[name] = &value
}

// Credential is the credential for Key Vault and the state blob, in
// appkit's priority order: a pre-fetched Key Vault token
// (AZURE_KEYVAULT_TOKEN, for a container with no `az`), the user-assigned
// identity named by AZURE_CLIENT_ID, then DefaultAzureCredential.
func Credential() (azcore.TokenCredential, error) {
	s := Get()
	mu.Lock()
	defer mu.Unlock()
	if !credDone {
		credDone = true
		switch {
		case s.AzureKeyVaultToken != "":
			cred = NewStaticTokenCredential(s.AzureKeyVaultToken, KeyVaultScope)
		case s.AzureClientID != "":
			cred, credErr = azidentity.NewManagedIdentityCredential(&azidentity.ManagedIdentityCredentialOptions{
				ID: azidentity.ClientID(s.AzureClientID),
			})
		default:
			cred, credErr = azidentity.NewDefaultAzureCredential(nil)
		}
	}
	return cred, credErr
}

// StaticTokenCredential wraps one pre-fetched access token. It refuses a
// scope it was not issued for, and expires an hour after it is made, as
// appkit's does: an expired token then fails as a 401, which is honest.
type StaticTokenCredential struct {
	Token   string
	Scope   string
	Expires time.Time
}

// NewStaticTokenCredential wraps token, expiring an hour from now.
func NewStaticTokenCredential(token, scope string) StaticTokenCredential {
	return StaticTokenCredential{Token: token, Scope: scope, Expires: time.Now().Add(time.Hour)}
}

// GetToken implements azcore.TokenCredential.
func (c StaticTokenCredential) GetToken(_ context.Context, opts policy.TokenRequestOptions) (azcore.AccessToken, error) {
	if len(opts.Scopes) > 0 {
		ok := false
		for _, s := range opts.Scopes {
			if s == c.Scope {
				ok = true
			}
		}
		if !ok {
			return azcore.AccessToken{}, fmt.Errorf("this token is scoped to %s, not %s",
				c.Scope, strings.Join(opts.Scopes, ", "))
		}
	}
	return azcore.AccessToken{Token: c.Token, ExpiresOn: c.Expires}, nil
}
