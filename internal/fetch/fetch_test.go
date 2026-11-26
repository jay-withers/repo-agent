package fetch

import (
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync/atomic"
	"testing"
	"time"
)

func noSleep(t *testing.T) *[]time.Duration {
	t.Helper()
	var slept []time.Duration
	restore := Sleep
	t.Cleanup(func() { Sleep = restore })
	Sleep = func(d time.Duration) { slept = append(slept, d) }
	return &slept
}

// sequence answers each request with the next status/body, failing the test
// on one request too many.
func sequence(t *testing.T, replies ...func(w http.ResponseWriter)) (*httptest.Server, *atomic.Int32) {
	t.Helper()
	var calls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		n := int(calls.Add(1))
		if n > len(replies) {
			t.Errorf("unexpected extra request %d to %s", n, r.URL)
			w.WriteHeader(500)
			return
		}
		replies[n-1](w)
	}))
	t.Cleanup(srv.Close)
	return srv, &calls
}

func status(code int, body string, headers ...string) func(http.ResponseWriter) {
	return func(w http.ResponseWriter) {
		for i := 0; i+1 < len(headers); i += 2 {
			w.Header().Set(headers[i], headers[i+1])
		}
		w.WriteHeader(code)
		_, _ = io.WriteString(w, body)
	}
}

func TestGetJSONRetriesARateLimitAndHonoursRetryAfter(t *testing.T) {
	slept := noSleep(t)
	srv, calls := sequence(t, status(403, "slow down", "Retry-After", "7"), status(502, ""), status(200, `{"ok": 1}`))
	got, err := GetJSON(NewClient(), srv.URL, nil, Params{{"a", "b c"}})
	if err != nil {
		t.Fatal(err)
	}
	if m, _ := got.(map[string]any); m["ok"] == nil {
		t.Errorf("got %v", got)
	}
	if calls.Load() != 3 {
		t.Errorf("%d calls", calls.Load())
	}
	// 7 from the header, then 2 s * attempt 2 from the linear backoff.
	if len(*slept) != 2 || (*slept)[0] != 7*time.Second || (*slept)[1] != 4*time.Second {
		t.Errorf("slept %v", *slept)
	}
}

func TestRetryAfterIsCappedAndNonDigitsIgnored(t *testing.T) {
	big := &http.Response{Header: http.Header{"Retry-After": {"3600"}}}
	if d := retryDelay(big, 1); d != 60*time.Second {
		t.Errorf("capped at a minute, got %v", d)
	}
	date := &http.Response{Header: http.Header{"Retry-After": {"Wed, 21 Oct 2026 07:28:00 GMT"}}}
	if d := retryDelay(date, 3); d != 6*time.Second {
		t.Errorf("a date falls back to the backoff, got %v", d)
	}
}

func TestGetJSONGivesUpAfterThreeAttempts(t *testing.T) {
	noSleep(t)
	srv, calls := sequence(t, status(503, ""), status(503, ""), status(503, ""))
	_, err := GetJSON(NewClient(), srv.URL, nil, nil)
	var fe *Error
	if !errors.As(err, &fe) || !strings.Contains(err.Error(), "failed after 3 attempts") {
		t.Errorf("got %v", err)
	}
	if calls.Load() != 3 {
		t.Errorf("%d calls", calls.Load())
	}
}

func TestGetJSONDoesNotRetryANotFound(t *testing.T) {
	noSleep(t)
	srv, calls := sequence(t, status(404, ""))
	if _, err := GetJSON(NewClient(), srv.URL, nil, nil); err == nil || !strings.HasPrefix(err.Error(), "404 from") {
		t.Errorf("got %v", err)
	}
	if calls.Load() != 1 {
		t.Errorf("%d calls", calls.Load())
	}
}

func TestInvalidJSONIsAnErrorNotARetry(t *testing.T) {
	noSleep(t)
	srv, calls := sequence(t, status(200, "<html>"))
	if _, err := GetJSON(NewClient(), srv.URL, nil, nil); err == nil {
		t.Error("expected an error")
	}
	if calls.Load() != 1 {
		t.Errorf("%d calls", calls.Load())
	}
}

func TestTransportErrorsAreRetried(t *testing.T) {
	noSleep(t)
	srv := httptest.NewServer(http.NotFoundHandler())
	url := srv.URL
	srv.Close()
	if _, err := GetJSON(NewClient(), url, nil, nil); err == nil || !strings.Contains(err.Error(), "failed after 3 attempts") {
		t.Errorf("got %v", err)
	}
}

func TestPostJSONIsNeverRetried(t *testing.T) {
	noSleep(t)
	srv, calls := sequence(t, status(503, strings.Repeat("x", 300)))
	_, err := PostJSON(NewClient(), srv.URL, map[string]any{"a": 1}, map[string]string{"X-Test": "1"})
	if err == nil || !strings.HasPrefix(err.Error(), "503 from") || len(err.Error()) > 300 {
		t.Errorf("got %v", err)
	}
	if calls.Load() != 1 {
		t.Errorf("%d calls: a POST must not be retried", calls.Load())
	}
	srv2, _ := sequence(t, status(200, `{"id": "x"}`))
	if got, err := PostJSON(NewClient(), srv2.URL, map[string]any{}, nil); err != nil || got.(map[string]any)["id"] != "x" {
		t.Errorf("got %v %v", got, err)
	}
	if _, err := PostJSON(NewClient(), "http://127.0.0.1:1", map[string]any{}, nil); err == nil {
		t.Error("a refused connection is an error")
	}
	if _, err := PostJSON(NewClient(), srv2.URL, map[string]any{"f": func() {}}, nil); err == nil {
		t.Error("an unencodable body is an error")
	}
}

func TestGraphQLErrorsInA200AreRetriedThenReported(t *testing.T) {
	noSleep(t)
	body := `{"errors": [{"message": "one"}, "two", {"type": "x"}, {"message": "four"}]}`
	srv, calls := sequence(t, status(200, body), status(200, body), status(200, body))
	_, err := GraphQL(NewClient(), srv.URL, "query {}", nil, nil)
	if err == nil || !strings.Contains(err.Error(), "graphql errors from") ||
		!strings.Contains(err.Error(), "one; two; map[type:x]") || strings.Contains(err.Error(), "four") {
		t.Errorf("got %v", err)
	}
	if calls.Load() != 3 {
		t.Errorf("%d calls", calls.Load())
	}
}

func TestGraphQLReturnsData(t *testing.T) {
	noSleep(t)
	srv, _ := sequence(t, status(200, `{"data": {"r0": {"name": "x"}}}`))
	got, err := GraphQL(NewClient(), srv.URL, "query {}", map[string]any{"o0": "a"}, nil)
	if err != nil || got.(map[string]any)["r0"] == nil {
		t.Errorf("got %v %v", got, err)
	}
	if _, err := GraphQL(NewClient(), srv.URL, "q", map[string]any{"f": func() {}}, nil); err == nil {
		t.Error("unencodable variables are an error")
	}
}

func TestWithParamsAppendsToAnExistingQuery(t *testing.T) {
	if got := withParams("https://x/y?a=1", Params{{"b", "2"}}); got != "https://x/y?a=1&b=2" {
		t.Errorf("got %s", got)
	}
	if isDigits("") {
		t.Error("empty is not digits")
	}
}
