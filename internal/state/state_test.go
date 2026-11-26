package state

import (
	"bytes"
	"context"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"github.com/Azure/azure-sdk-for-go/sdk/storage/azblob/blockblob"

	"github.com/jay-withers/repo-agent/internal/model"
	"github.com/jay-withers/repo-agent/internal/settings"
)

var day = time.Date(2026, 11, 29, 18, 0, 0, 0, time.UTC)

func f(repo, check string) model.Finding {
	return model.Finding{Repo: repo, Check: check, Severity: "low", Title: check + " title"}
}

func TestFindingIDIsTheFirstTwelveOfSha256(t *testing.T) {
	// sha256("o/a:hygiene.no_readme"), as the Python computed it.
	if id := f("o/a", "hygiene.no_readme").ID(); id != "1885f6c1129b" {
		t.Errorf("got %s", id)
	}
}

func TestReconcile(t *testing.T) {
	model.Now = func() time.Time { return day }
	t.Cleanup(func() { model.Now = func() time.Time { return time.Now().UTC() } })
	kept, fresh, held, expired := f("o/a", "kept"), f("o/a", "fresh"), f("o/a", "held"), f("o/a", "expired")
	gone := KnownFinding{FirstSeen: "2026-01-01", Repo: "o/z", Check: "gone", Title: "gone"}
	prior := State{
		Findings: map[string]KnownFinding{
			kept.ID():      {FirstSeen: "2026-10-01"},
			"zzzzzzzzzzzz": gone,
		},
		Suppressed: map[string]Suppression{
			held.ID():     {Reason: "ok", Until: model.Ptr("2026-11-29")},
			expired.ID():  {Reason: "ok", Until: model.Ptr("2026-11-28")},
			"orphan00000": {Reason: "nothing found"},
		},
	}
	r := Reconcile(prior, []model.Finding{kept, fresh, held, expired}, day)
	if len(r.Findings) != 3 || len(r.Suppressed) != 1 || r.Suppressed[0].Check != "held" {
		t.Fatalf("reported %v, held %v", r.Findings, r.Suppressed)
	}
	if r.Findings[0].IsNew || r.Findings[0].FirstSeen != "2026-10-01" || !r.Findings[1].IsNew || r.Findings[1].FirstSeen != "2026-11-29" {
		t.Errorf("annotations %+v", r.Findings)
	}
	if len(r.New()) != 2 {
		t.Errorf("new %v", r.New())
	}
	if len(r.Resolved) != 1 || r.Resolved[0] != gone {
		t.Errorf("resolved %v", r.Resolved)
	}
	if _, ok := r.State.Suppressed[held.ID()]; !ok || len(r.State.Suppressed) != 1 {
		t.Errorf("expired and orphaned suppressions are dropped: %v", r.State.Suppressed)
	}
	if len(r.State.Findings) != 4 || *r.State.LastRun != "2026-11-29T18:00:00+00:00" {
		t.Errorf("carried %v, last run %v", r.State.Findings, *r.State.LastRun)
	}
}

func TestAnUnparseableExpiryExpires(t *testing.T) {
	if (Suppression{Until: model.Ptr("next week")}).Active(day) {
		t.Error("a typo must not suppress for ever")
	}
	if !(Suppression{Until: model.Ptr("20261129")}).Active(day) || !(Suppression{}).Active(day) {
		t.Error("compact dates and no expiry are active")
	}
}

func TestJSONRoundTrip(t *testing.T) {
	s := Empty()
	s.LastRun = model.Ptr("2026-11-29T18:00:00+00:00")
	s.Findings["b"] = KnownFinding{FirstSeen: "2026-01-01", Repo: "o/b", Check: "c", Title: "ü"}
	s.Findings["a"] = KnownFinding{FirstSeen: "2026-01-02"}
	s.Suppressed["a"] = Suppression{Reason: "why", Added: "2026-01-03"}
	text := s.ToJSON()
	if !strings.HasPrefix(text, "{\n  \"version\": 1,\n  \"last_run\": \"2026-11-29T18:00:00+00:00\",\n  \"findings\": {\n    \"a\":") ||
		!strings.Contains(text, "\"title\": \"\\u00fc\"") || !strings.Contains(text, `"until": null`) {
		t.Errorf("not the Python's shape:\n%s", text)
	}
	back, err := FromJSON([]byte(text))
	if err != nil || back.Findings["b"] != s.Findings["b"] || back.Suppressed["a"].Reason != "why" || *back.LastRun != *s.LastRun {
		t.Errorf("round trip: %+v %v", back, err)
	}
	if Empty().ToJSON() != "{\n  \"version\": 1,\n  \"last_run\": null,\n  \"findings\": {},\n  \"suppressed\": {}\n}" {
		t.Errorf("empty: %s", Empty().ToJSON())
	}
}

func TestFromJSONTolerance(t *testing.T) {
	s, err := FromJSON([]byte(`{"findings": {"x": {"repo": "no first_seen"}, "y": {"first_seen": "2026-01-01", "extra": 1}},
		"suppressed": {"z": {"reason": "r", "until": 20261231}, "w": null}}`))
	if err != nil || len(s.Findings) != 1 || *s.Suppressed["z"].Until != "20261231" || len(s.Suppressed) != 1 {
		t.Errorf("got %+v %v", s, err)
	}
	// A hand edit costs a field, never the document (which the next scan
	// would overwrite, losing every suppression).
	kept, err := FromJSON([]byte(`{"version": 1.0, "suppressed": {"a": {"until": null}, "b": {"reason": null}}}`))
	if err != nil || len(kept.Suppressed) != 2 {
		t.Errorf("got %+v %v", kept, err)
	}
	for _, bad := range []string{`not json`, `{"version": 2}`, `{"version": 1.5}`, `{"version": "one"}`} {
		if _, err := FromJSON([]byte(bad)); err == nil {
			t.Errorf("%s should be refused", bad)
		}
	}
}

type fakeStore struct {
	data     []byte
	found    bool
	readErr  error
	writeErr error
	written  []byte
}

func (s *fakeStore) Read(context.Context) ([]byte, bool, error) { return s.data, s.found, s.readErr }
func (s *fakeStore) Write(_ context.Context, d []byte) error {
	s.written = d
	return s.writeErr
}

func useStore(t *testing.T, s Store, err error) {
	restore := DefaultStore
	t.Cleanup(func() { DefaultStore = restore })
	DefaultStore = func() (Store, error) { return s, err }
}

func TestLoadNeverFails(t *testing.T) {
	cases := map[string]Store{
		"missing":    &fakeStore{},
		"read error": &fakeStore{readErr: errors.New("403")},
		"unreadable": &fakeStore{data: []byte("{"), found: true},
	}
	for name, store := range cases {
		useStore(t, store, nil)
		if s := Load(); len(s.Findings) != 0 {
			t.Errorf("%s: %+v", name, s)
		}
	}
	useStore(t, nil, errors.New("no credential"))
	if s := Load(); s.Findings == nil {
		t.Error("an empty state, never nil maps")
	}
	useStore(t, &fakeStore{data: []byte(`{"findings": {"a": {"first_seen": "2026-01-01"}}}`), found: true}, nil)
	if s := Load(); len(s.Findings) != 1 {
		t.Errorf("got %+v", s)
	}
}

func TestSaveReportsWhetherItLanded(t *testing.T) {
	store := &fakeStore{}
	useStore(t, store, nil)
	if !Save(Empty()) || !bytes.HasPrefix(store.written, []byte("{\n  \"version\": 1")) {
		t.Error("save")
	}
	useStore(t, &fakeStore{writeErr: errors.New("409")}, nil)
	if Save(Empty()) {
		t.Error("a failed write is false")
	}
	useStore(t, nil, nil)
	if Save(Empty()) {
		t.Error("no store is false")
	}
	useStore(t, nil, errors.New("x"))
	if Save(Empty()) {
		t.Error("a broken store is false")
	}
}

func TestDefaultStore(t *testing.T) {
	t.Chdir(t.TempDir())
	t.Setenv("STATE_CONTAINER_URL", "")
	settings.Reset()
	t.Cleanup(settings.Reset)
	if s, err := DefaultStore(); s != nil || err != nil {
		t.Errorf("off without a URL: %v %v", s, err)
	}
	t.Setenv("STATE_CONTAINER_URL", "https://acct.blob.core.windows.net/state/")
	t.Setenv("AZURE_KEYVAULT_TOKEN", "tok")
	settings.Reset()
	if s, err := DefaultStore(); s == nil || err != nil {
		t.Errorf("a blob store: %v %v", s, err)
	}
}

// TestBlobStore talks to a minimal fake of the Blob service: a 404 is "no
// document yet", anything else is an error, and a PUT is the upload.
func TestBlobStore(t *testing.T) {
	var uploaded []byte
	mode := "ok"
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		switch {
		case r.Method == http.MethodPut:
			buf := new(bytes.Buffer)
			_, _ = buf.ReadFrom(r.Body)
			uploaded = buf.Bytes()
			w.WriteHeader(http.StatusCreated)
		case mode == "missing":
			w.Header().Set("x-ms-error-code", "BlobNotFound")
			w.WriteHeader(http.StatusNotFound)
		case mode == "denied":
			w.Header().Set("x-ms-error-code", "AuthorizationFailure")
			w.WriteHeader(http.StatusForbidden)
		default:
			w.Header().Set("Content-Length", "2")
			_, _ = w.Write([]byte("{}"))
		}
	}))
	t.Cleanup(srv.Close)
	client, err := blockblob.NewClientWithNoCredential(srv.URL+"/state/state.json", nil)
	if err != nil {
		t.Fatal(err)
	}
	store := blobStore{client}
	ctx := context.Background()
	if data, found, err := store.Read(ctx); err != nil || !found || string(data) != "{}" {
		t.Errorf("read: %q %v %v", data, found, err)
	}
	mode = "missing"
	if _, found, err := store.Read(ctx); err != nil || found {
		t.Errorf("missing: %v %v", found, err)
	}
	mode = "denied"
	if _, _, err := store.Read(ctx); err == nil {
		t.Error("denied is an error")
	}
	if err := store.Write(ctx, []byte(`{"a":1}`)); err != nil || string(uploaded) != `{"a":1}` {
		t.Errorf("write: %v %q", err, uploaded)
	}
}
