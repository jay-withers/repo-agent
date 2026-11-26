// Package mailer sends the digest through Resend.
//
// A provider failure is an outcome, not an error: sending is the last thing
// a run does, and a mail provider having a bad minute should not turn a
// successful scan into a failed job. A secret that cannot be resolved is an
// error, as the Python's raised: nothing was attempted, so the run must fail
// and must not save its findings as seen. The recipient is never logged or
// returned; it lives in Key Vault to stay out of anything shared.
package mailer

import (
	"fmt"
	"net/http"
	"strings"

	"github.com/jay-withers/repo-agent/internal/fetch"
	"github.com/jay-withers/repo-agent/internal/jv"
	"github.com/jay-withers/repo-agent/internal/logging"
	"github.com/jay-withers/repo-agent/internal/pyfmt"
	"github.com/jay-withers/repo-agent/internal/settings"
)

var log = logging.Named("repoagent.mailer")

// ResendEndpoint is Resend's send API.
var ResendEndpoint = "https://api.resend.com/emails"

// Result is the outcome of one send: sent, failed or skipped.
type Result struct {
	Status     string
	ProviderID string
	Error      string
}

// Send sends the digest. "skipped" when no DIGEST-EMAIL-TO is configured,
// the development default.
func Send(client *http.Client, subject, html, text string) (Result, error) {
	recipient, err := settings.OptionalSecret("DIGEST-EMAIL-TO")
	if err != nil {
		return Result{}, err
	}
	if recipient == "" {
		log.Info("no DIGEST-EMAIL-TO configured, not sending")
		return Result{Status: "skipped"}, nil
	}
	var addresses []string
	for _, a := range strings.Split(recipient, ",") {
		if a = strings.TrimSpace(a); a != "" {
			addresses = append(addresses, a)
		}
	}
	// Resend rejects a non-ASCII address with a 422 naming the address, not
	// the character. Which recipient and which character, never the address.
	for n, address := range addresses {
		for _, r := range address {
			if r > 0x7f {
				detail := fmt.Sprintf("recipient %d of %d contains U+%04X", n+1, len(addresses), r)
				log.Warn("DIGEST-EMAIL-TO is not plain ASCII: " + detail)
				return Result{Status: "failed", Error: "recipient is not plain ASCII: " + detail}, nil
			}
		}
	}
	key, err := settings.Secret("RESEND-API-KEY")
	if err != nil {
		return Result{}, err
	}
	raw, err := fetch.PostJSON(client, ResendEndpoint, map[string]any{
		"from":    settings.Get().DigestEmailFrom,
		"to":      addresses,
		"subject": subject,
		"html":    html,
		"text":    text,
	}, map[string]string{"Authorization": "Bearer " + key, "Content-Type": "application/json"})
	if err != nil {
		log.Warn(fmt.Sprintf("digest email failed: %s", err))
		detail, _ := pyfmt.Truncate(err.Error(), 500)
		return Result{Status: "failed", Error: detail}, nil
	}
	return Result{Status: "sent", ProviderID: jv.Str(jv.Obj(raw), "id")}, nil
}
