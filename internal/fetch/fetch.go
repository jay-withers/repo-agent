// Package fetch is the shared HTTP plumbing for the outbound APIs: GitHub,
// DeepSeek and Resend.
//
// The rules are the ones the Python implementation had, first copied from
// market-agent:
//
//   - GET and GraphQL are retried on RetryableStatus and on transport
//     errors, three attempts, linear backoff, honouring Retry-After (capped at
//     60 s). 403 is retryable because GitHub signals its secondary rate limit
//     with 403, not 429.
//   - GraphQL is retried despite being a POST, because a read has no side
//     effect, and its 200-with-errors body is an error, not an empty result.
//   - PostJSON is never retried: the only other POST sends an email, and a
//     duplicate digest is worse than a missing one.
package fetch

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"time"

	"github.com/jay-withers/repo-agent/internal/logging"
	"github.com/jay-withers/repo-agent/internal/pyfmt"
)

var log = logging.Named("repoagent.fetch")

// Timeout bounds every request, so a hung connection cannot eat the job's
// replica timeout.
const Timeout = 30 * time.Second

// DefaultAttempts and Backoff are the retry policy.
const (
	DefaultAttempts = 3
	Backoff         = 2 * time.Second
)

// RetryableStatus are the statuses worth another attempt. 520-524 are
// Cloudflare's: the edge answered and its origin did not.
var RetryableStatus = map[int]bool{
	403: true, 408: true, 429: true, 500: true, 502: true, 503: true, 504: true,
	520: true, 521: true, 522: true, 523: true, 524: true,
}

// Sleep is how a retry waits. Tests replace it.
var Sleep = time.Sleep

// Error is a request that failed, after its retries where it had any.
type Error struct{ Message string }

func (e *Error) Error() string { return e.Message }

func errorf(format string, args ...any) error { return &Error{fmt.Sprintf(format, args...)} }

// NewClient is the client every caller shares, with the timeout set.
func NewClient() *http.Client { return &http.Client{Timeout: Timeout} }

func retryDelay(resp *http.Response, attempt int) time.Duration {
	if resp != nil {
		if ra := resp.Header.Get("Retry-After"); ra != "" && isDigits(ra) {
			n, _ := strconv.Atoi(ra)
			return min(time.Duration(n)*time.Second, 60*time.Second)
		}
	}
	return Backoff * time.Duration(attempt)
}

func isDigits(s string) bool {
	for _, r := range s {
		if r < '0' || r > '9' {
			return false
		}
	}
	return s != ""
}

// Params are query parameters, kept in the order given.
type Params [][2]string

func withParams(raw string, params Params) string {
	if len(params) == 0 {
		return raw
	}
	parts := make([]string, len(params))
	for i, p := range params {
		parts[i] = url.QueryEscape(p[0]) + "=" + url.QueryEscape(p[1])
	}
	sep := "?"
	if strings.Contains(raw, "?") {
		sep = "&"
	}
	return raw + sep + strings.Join(parts, "&")
}

func do(client *http.Client, method, target string, headers map[string]string, body []byte) (*http.Response, []byte, error) {
	var reader io.Reader
	if body != nil {
		reader = bytes.NewReader(body)
	}
	req, err := http.NewRequest(method, target, reader) //nolint:noctx // bounded by the client's Timeout
	if err != nil {
		return nil, nil, err
	}
	if body != nil {
		req.Header.Set("Content-Type", "application/json")
	}
	for k, v := range headers {
		req.Header.Set(k, v)
	}
	resp, err := client.Do(req)
	if err != nil {
		return nil, nil, err
	}
	defer func() { _ = resp.Body.Close() }()
	data, err := io.ReadAll(resp.Body)
	if err != nil {
		return resp, nil, err
	}
	return resp, data, nil
}

func success(status int) bool { return status >= 200 && status < 300 }

// retrying runs one request up to `attempts` times. parse turns a 2xx body
// into the result, or returns a *Error to retry.
func retrying(client *http.Client, method, target string, headers map[string]string, body []byte, attempts int, parse func([]byte) (any, error)) (any, error) {
	var last error
	for attempt := 1; attempt <= attempts; attempt++ {
		resp, data, err := do(client, method, target, headers, body)
		if err == nil {
			switch {
			case RetryableStatus[resp.StatusCode]:
				err = errorf("%d from %s", resp.StatusCode, target)
			case !success(resp.StatusCode):
				// Not retried: a 401 or a 404 will say the same thing again.
				return nil, errorf("%d from %s", resp.StatusCode, target)
			default:
				result, perr := parse(data)
				var fe *Error
				if perr == nil || !errors.As(perr, &fe) {
					return result, perr
				}
				err = perr
			}
		}
		last = err
		if attempt == attempts {
			break
		}
		delay := retryDelay(resp, attempt)
		log.Warn(fmt.Sprintf("%s (attempt %d/%d), retrying in %.0fs", err, attempt, attempts, delay.Seconds()))
		Sleep(delay)
	}
	return nil, errorf("%s failed after %d attempts: %s", target, attempts, last)
}

// GetJSON GETs target and decodes the JSON, retrying transient failures.
func GetJSON(client *http.Client, target string, headers map[string]string, params Params) (any, error) {
	return retrying(client, http.MethodGet, withParams(target, params), headers, nil, DefaultAttempts, decode)
}

func decode(data []byte) (any, error) {
	var v any
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber()
	if err := dec.Decode(&v); err != nil {
		return nil, fmt.Errorf("invalid JSON: %w", err)
	}
	return v, nil
}

// PostJSON POSTs body as JSON and decodes the reply. Never retried.
func PostJSON(client *http.Client, target string, body any, headers map[string]string) (any, error) {
	payload, err := json.Marshal(body)
	if err != nil {
		return nil, err
	}
	return PostRaw(client, target, payload, headers)
}

// PostRaw is PostJSON with the body already encoded, for a caller whose
// bytes must be exactly its own.
func PostRaw(client *http.Client, target string, payload []byte, headers map[string]string) (any, error) {
	resp, data, err := do(client, http.MethodPost, target, headers, payload)
	if err != nil {
		return nil, errorf("%s failed: %s", target, err)
	}
	if !success(resp.StatusCode) {
		detail, _ := pyfmt.Truncate(string(data), 200)
		return nil, errorf("%d from %s: %s", resp.StatusCode, target, detail)
	}
	return decode(data)
}

// GraphQL POSTs a query and returns its `data`, retrying transient failures
// and treating a body with `errors` as one.
func GraphQL(client *http.Client, target, query string, variables map[string]any, headers map[string]string) (any, error) {
	if variables == nil {
		variables = map[string]any{}
	}
	payload, err := json.Marshal(map[string]any{"query": query, "variables": variables})
	if err != nil {
		return nil, err
	}
	return retrying(client, http.MethodPost, target, headers, payload, DefaultAttempts, func(data []byte) (any, error) {
		v, err := decode(data)
		if err != nil {
			return nil, err
		}
		obj, _ := v.(map[string]any)
		if errs, ok := obj["errors"].([]any); ok && len(errs) > 0 {
			var messages []string
			for i, e := range errs {
				if i == 3 {
					break
				}
				if m, ok := e.(map[string]any); ok {
					if msg, ok := m["message"]; ok {
						messages = append(messages, fmt.Sprint(msg))
						continue
					}
				}
				messages = append(messages, fmt.Sprint(e))
			}
			return nil, errorf("graphql errors from %s: %s", target, strings.Join(messages, "; "))
		}
		return obj["data"], nil
	})
}
