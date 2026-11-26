// Package github reads GitHub as the installed App: authentication, the
// REST and GraphQL calls, and parsing their JSON into model types.
//
// **No search API, ever.** Its secondary rate limit answers 403, which fetch
// retries, and four search calls per repository across eleven repositories
// tripped it within seconds during the Python design.
package github

import (
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/golang-jwt/jwt/v5"

	"github.com/jay-withers/repo-agent/internal/fetch"
	"github.com/jay-withers/repo-agent/internal/jv"
	"github.com/jay-withers/repo-agent/internal/logging"
	"github.com/jay-withers/repo-agent/internal/pyfmt"
	"github.com/jay-withers/repo-agent/internal/settings"
)

var authLog = logging.Named("repoagent.github.auth")

// GitHub rejects a JWT whose exp is more than ten minutes out, or whose iat
// is in its future. Nine minutes, backdated one.
const (
	jwtLifetime       = 9 * time.Minute
	jwtBackdate       = 60 * time.Second
	tokenRefreshAhead = 120 * time.Second
)

var (
	authMu sync.Mutex
	cache  *installationToken
	// now is the clock the token cache checks expiry against.
	now = time.Now
)

type installationToken struct {
	token       string
	expiresAt   time.Time
	permissions map[string]string
}

// ResetCache drops the cached installation token. For tests.
func ResetCache() {
	authMu.Lock()
	defer authMu.Unlock()
	cache = nil
}

// AppJWT signs the short-lived JWT that proves "I am this App".
func AppJWT(at time.Time) (string, error) {
	appID, err := settings.Secret("GITHUB-APP-ID")
	if err != nil {
		return "", err
	}
	pem, err := settings.Secret("GITHUB-APP-PRIVATE-KEY")
	if err != nil {
		return "", err
	}
	key, err := jwt.ParseRSAPrivateKeyFromPEM([]byte(pem))
	if err != nil {
		return "", fmt.Errorf("GITHUB-APP-PRIVATE-KEY is not an RSA PEM: %w", err)
	}
	issued := at.Unix()
	claims := jwt.MapClaims{
		"iat": issued - int64(jwtBackdate.Seconds()),
		"exp": issued + int64(jwtLifetime.Seconds()),
		"iss": appID,
	}
	return jwt.NewWithClaims(jwt.SigningMethodRS256, claims).SignedString(key)
}

func appHeaders(token string) map[string]string {
	return map[string]string{
		"Authorization":        "Bearer " + token,
		"Accept":               "application/vnd.github+json",
		"X-GitHub-Api-Version": "2022-11-28",
	}
}

// InstallationID finds the App's single installation. Looked up rather than
// configured: one fewer secret to rotate.
func InstallationID(client *http.Client, appJWT string) (int, error) {
	raw, err := fetch.GetJSON(client, settings.Get().GitHubAPIURL+"/app/installations", appHeaders(appJWT), nil)
	if err != nil {
		return 0, err
	}
	installations := jv.List(raw)
	if len(installations) == 0 {
		return 0, fmt.Errorf("this GitHub App has no installations. Install it on the account " +
			"whose repositories should be scanned")
	}
	first := jv.Obj(installations[0])
	if len(installations) > 1 {
		login := jv.Str(jv.Obj(first["account"]), "login")
		if login == "" {
			login = "?"
		}
		authLog.Warn(fmt.Sprintf("App has %d installations; using the first (%s)", len(installations), login))
	}
	id, ok := jv.Int(first["id"])
	if !ok {
		return 0, fmt.Errorf("installation has no id")
	}
	return id, nil
}

// Token is an installation access token, minted on demand and cached until
// two minutes before it expires.
func Token(client *http.Client) (string, error) {
	authMu.Lock()
	defer authMu.Unlock()
	moment := now().UTC()
	if cache != nil && cache.expiresAt.Add(-tokenRefreshAhead).After(moment) {
		return cache.token, nil
	}
	appJWT, err := AppJWT(now())
	if err != nil {
		return "", err
	}
	id, err := InstallationID(client, appJWT)
	if err != nil {
		return "", err
	}
	raw, err := fetch.PostJSON(client,
		fmt.Sprintf("%s/app/installations/%d/access_tokens", settings.Get().GitHubAPIURL, id),
		map[string]any{}, appHeaders(appJWT))
	if err != nil {
		return "", err
	}
	payload := jv.Obj(raw)
	token := jv.Str(payload, "token")
	if token == "" {
		return "", fmt.Errorf("access token response carries no token")
	}
	expires := parseExpiry(payload["expires_at"], moment)
	perms := map[string]string{}
	for k, v := range jv.Obj(payload["permissions"]) {
		perms[k] = fmt.Sprint(v)
	}
	cache = &installationToken{token: token, expiresAt: expires, permissions: perms}
	authLog.Info("minted installation token, expires " + pyfmt.ISO(expires))
	return token, nil
}

// parseExpiry reads GitHub's expires_at, falling back to the documented
// hour: a token whose expiry cannot be read is still a usable token.
func parseExpiry(raw any, from time.Time) time.Time {
	if s, ok := raw.(string); ok {
		if t, err := time.Parse(time.RFC3339, strings.Replace(s, "Z", "+00:00", 1)); err == nil {
			return t
		}
		authLog.Warn(fmt.Sprintf("could not parse token expiry %s, assuming one hour", pyfmt.Repr(s)))
	}
	return from.Add(time.Hour)
}

// Permitted reports whether the installation token carries `name` at any
// level. Read from the token's grant rather than probed: a forbidden API
// answers 403, which fetch would retry as a rate limit.
func Permitted(client *http.Client, name string) (bool, error) {
	if _, err := Token(client); err != nil {
		return false, err
	}
	authMu.Lock()
	defer authMu.Unlock()
	if cache == nil {
		return false, nil
	}
	_, ok := cache.permissions[name]
	return ok, nil
}

// AuthHeaders are the headers for an ordinary call as the installation.
func AuthHeaders(client *http.Client) (map[string]string, error) {
	token, err := Token(client)
	if err != nil {
		return nil, err
	}
	return appHeaders(token), nil
}
