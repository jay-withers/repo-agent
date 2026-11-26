// Package state is what the scan remembers between runs: one JSON document
// in a blob, read whole at the start of a run and written whole at the end.
//
// It answers what is new, and what has been decided to live with
// (suppressions). **Absent state is not an error**: every reason for
// failing to read it degrades to "everything looks new".
//
// The document's shape is the Python original's, byte for byte (written by
// pyfmt.JSON as json.dumps(indent=2)), so the cutover reads last week's
// document and keeps every suppression and every first_seen date.
package state

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/Azure/azure-sdk-for-go/sdk/storage/azblob/blob"
	"github.com/Azure/azure-sdk-for-go/sdk/storage/azblob/bloberror"
	"github.com/Azure/azure-sdk-for-go/sdk/storage/azblob/blockblob"

	"github.com/jay-withers/repo-agent/internal/logging"
	"github.com/jay-withers/repo-agent/internal/model"
	"github.com/jay-withers/repo-agent/internal/pyfmt"
	"github.com/jay-withers/repo-agent/internal/settings"
)

var log = logging.Named("repoagent.state")

// BlobName is the document's name inside the container.
const BlobName = "state.json"

// SchemaVersion is bumped when the shape changes incompatibly. A document
// from the future is refused rather than overwritten.
const SchemaVersion = 1

// KnownFinding is a finding a previous run saw. repo, check and title are
// kept so a resolved finding can still be named.
type KnownFinding struct {
	FirstSeen string
	Repo      string
	Check     string
	Title     string
}

// Suppression is a decision to stop reporting something, and why.
type Suppression struct {
	Reason string
	// ISO date, or nil for indefinitely.
	Until *string
	Added string
}

// Active reports whether the suppression still applies on `today`. An
// unparseable expiry expires: a typo must not suppress a finding for ever.
func (s Suppression) Active(today time.Time) bool {
	if s.Until == nil {
		return true
	}
	until, ok := ParseDate(*s.Until)
	if !ok {
		log.Warn(fmt.Sprintf("suppression expiry %s is not a date, ignoring it", pyfmt.Repr(*s.Until)))
		return false
	}
	return !until.Before(truncateDay(today))
}

// ParseDate is date.fromisoformat for the two forms it accepts that anyone
// would type: YYYY-MM-DD and YYYYMMDD.
func ParseDate(s string) (time.Time, bool) {
	for _, layout := range []string{"2006-01-02", "20060102"} {
		if t, err := time.Parse(layout, s); err == nil {
			return t, true
		}
	}
	return time.Time{}, false
}

func truncateDay(t time.Time) time.Time {
	y, m, d := t.UTC().Date()
	return time.Date(y, m, d, 0, 0, 0, 0, time.UTC)
}

// State is everything carried from one run to the next.
type State struct {
	Findings   map[string]KnownFinding
	Suppressed map[string]Suppression
	LastRun    *string
}

// Empty is a state with nothing in it.
func Empty() State {
	return State{Findings: map[string]KnownFinding{}, Suppressed: map[string]Suppression{}}
}

func sortedKeys[V any](m map[string]V) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

// ToJSON is the document, exactly as the Python's json.dumps(indent=2).
func (s State) ToJSON() string {
	findings := pyfmt.Object{}
	for _, k := range sortedKeys(s.Findings) {
		f := s.Findings[k]
		findings = append(findings, pyfmt.KV{Key: k, Value: pyfmt.Object{
			{Key: "first_seen", Value: f.FirstSeen},
			{Key: "repo", Value: f.Repo},
			{Key: "check", Value: f.Check},
			{Key: "title", Value: f.Title},
		}})
	}
	suppressed := pyfmt.Object{}
	for _, k := range sortedKeys(s.Suppressed) {
		r := s.Suppressed[k]
		suppressed = append(suppressed, pyfmt.KV{Key: k, Value: pyfmt.Object{
			{Key: "reason", Value: r.Reason},
			{Key: "until", Value: r.Until},
			{Key: "added", Value: r.Added},
		}})
	}
	return pyfmt.JSON(pyfmt.Object{
		{Key: "version", Value: SchemaVersion},
		{Key: "last_run", Value: s.LastRun},
		{Key: "findings", Value: findings},
		{Key: "suppressed", Value: suppressed},
	}, 2)
}

// FromJSON parses a state document, tolerating anything but invalid JSON
// and a version from the future.
func FromJSON(raw []byte) (State, error) {
	var payload map[string]json.RawMessage
	if err := json.Unmarshal(raw, &payload); err != nil {
		return State{}, err
	}
	version := float64(SchemaVersion)
	if v, ok := payload["version"]; ok && string(v) != "null" {
		if err := json.Unmarshal(v, &version); err != nil {
			return State{}, fmt.Errorf("state version is not a number: %w", err)
		}
	}
	if version > SchemaVersion {
		return State{}, fmt.Errorf("state document is version %v, this build understands %d. "+
			"Refusing to read it rather than overwriting it", version, SchemaVersion)
	}
	state := Empty()
	if v, ok := payload["last_run"]; ok {
		var last *string
		if err := json.Unmarshal(v, &last); err == nil {
			state.LastRun = last
		}
	}
	var findings map[string]map[string]any
	_ = json.Unmarshal(payload["findings"], &findings)
	for k, v := range findings {
		first, _ := v["first_seen"].(string)
		if first == "" {
			continue
		}
		repo, _ := v["repo"].(string)
		check, _ := v["check"].(string)
		title, _ := v["title"].(string)
		state.Findings[k] = KnownFinding{FirstSeen: first, Repo: repo, Check: check, Title: title}
	}
	var suppressed map[string]json.RawMessage
	_ = json.Unmarshal(payload["suppressed"], &suppressed)
	for k, v := range suppressed {
		var rule map[string]any
		if err := json.Unmarshal(v, &rule); err != nil || rule == nil {
			continue
		}
		// Kept even without a string reason. The Python failed the whole
		// document on a missing one, and the next scan then overwrote it,
		// losing every suppression: a hand edit must cost one field, not that.
		reason, _ := rule["reason"].(string)
		var until *string
		if u, ok := rule["until"].(string); ok {
			until = &u
		} else if n, ok := rule["until"].(float64); ok {
			// Not a date at all: kept, and expires on the first check.
			until = model.Ptr(strconv.FormatFloat(n, 'f', -1, 64))
		} else if rule["until"] != nil {
			until = model.Ptr(fmt.Sprint(rule["until"]))
		}
		added, _ := rule["added"].(string)
		state.Suppressed[k] = Suppression{Reason: reason, Until: until, Added: added}
	}
	return state, nil
}

// Reconciled is one run's findings placed against what the last run saw.
type Reconciled struct {
	Findings   []model.Finding
	Resolved   []KnownFinding
	Suppressed []model.Finding
	State      State
}

// New is the findings no previous run saw.
func (r Reconciled) New() []model.Finding {
	var out []model.Finding
	for _, f := range r.Findings {
		if f.IsNew {
			out = append(out, f)
		}
	}
	return out
}

// Reconcile is pure: annotate findings against prior, and produce the
// state to store. Suppressed findings are still found, so not resolved;
// expired and orphaned suppressions are dropped.
func Reconcile(prior State, findings []model.Finding, now time.Time) Reconciled {
	stamp := now.UTC().Format("2006-01-02")
	var reported, held []model.Finding
	carried := map[string]KnownFinding{}
	for _, f := range findings {
		id := f.ID()
		known, seen := prior.Findings[id]
		first := stamp
		if seen {
			first = known.FirstSeen
		}
		f.FirstSeen, f.IsNew = first, !seen
		carried[id] = KnownFinding{FirstSeen: first, Repo: f.Repo, Check: f.Check, Title: f.Title}
		if rule, ok := prior.Suppressed[id]; ok && rule.Active(now) {
			held = append(held, f)
		} else {
			reported = append(reported, f)
		}
	}
	var resolved []KnownFinding
	for _, id := range sortedKeys(prior.Findings) {
		if _, still := carried[id]; !still {
			resolved = append(resolved, prior.Findings[id])
		}
	}
	kept := map[string]Suppression{}
	for id, rule := range prior.Suppressed {
		if _, still := carried[id]; still && rule.Active(now) {
			kept[id] = rule
		}
	}
	last := pyfmt.ISO(model.Now())
	return Reconciled{
		Findings:   reported,
		Resolved:   resolved,
		Suppressed: held,
		State:      State{Findings: carried, Suppressed: kept, LastRun: &last},
	}
}

// Store is where the document lives. The blob store in Azure; a fake in tests.
type Store interface {
	// Read returns the document, or found=false when there is none yet.
	Read(ctx context.Context) (data []byte, found bool, err error)
	Write(ctx context.Context, data []byte) error
}

// DefaultStore is the configured store, or nil when STATE_CONTAINER_URL is
// empty (history off). A variable so tests can swap it.
var DefaultStore = func() (Store, error) {
	url := settings.Get().StateContainerURL
	if url == "" {
		return nil, nil
	}
	cred, err := settings.Credential()
	if err != nil {
		return nil, err
	}
	client, err := blockblob.NewClient(strings.TrimRight(url, "/")+"/"+BlobName, cred, nil)
	if err != nil {
		return nil, err
	}
	return blobStore{client}, nil
}

type blobStore struct{ client *blockblob.Client }

func (b blobStore) Read(ctx context.Context) ([]byte, bool, error) {
	resp, err := b.client.DownloadStream(ctx, nil)
	if err != nil {
		if bloberror.HasCode(err, bloberror.BlobNotFound, bloberror.ContainerNotFound) {
			return nil, false, nil
		}
		return nil, false, err
	}
	defer func() { _ = resp.Body.Close() }()
	data, err := io.ReadAll(resp.Body)
	return data, true, err
}

func (b blobStore) Write(ctx context.Context, data []byte) error {
	_, err := b.client.Upload(ctx, nopCloser{bytes.NewReader(data)}, &blockblob.UploadOptions{
		HTTPHeaders: &blob.HTTPHeaders{BlobContentType: model.Ptr("application/json")},
	})
	return err
}

type nopCloser struct{ *bytes.Reader }

func (nopCloser) Close() error { return nil }

// Load reads the state, or returns an empty one. Never fails: every reason
// for not reading it means the same thing to the caller.
func Load() State {
	store, err := DefaultStore()
	if err != nil {
		log.Warn(fmt.Sprintf("could not read state, treating every finding as new: %s", err))
		return Empty()
	}
	if store == nil {
		return Empty()
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	data, found, err := store.Read(ctx)
	switch {
	case err != nil:
		log.Warn(fmt.Sprintf("could not read state, treating every finding as new: %s", err))
		return Empty()
	case !found:
		log.Info("no state document yet; every finding will report as new")
		return Empty()
	}
	s, err := FromJSON(data)
	if err != nil {
		log.Warn(fmt.Sprintf("state document is unreadable, ignoring it: %s", err))
		return Empty()
	}
	last := "never"
	if s.LastRun != nil && *s.LastRun != "" {
		last = *s.LastRun
	}
	log.Info(fmt.Sprintf("state: %d known finding(s), %d suppression(s), last run %s",
		len(s.Findings), len(s.Suppressed), last))
	return s
}

// ErrNoStore is Save's answer when history is switched off.
var ErrNoStore = errors.New("no STATE_CONTAINER_URL configured")

// Save writes the state, reporting whether it landed. The scan ignores the
// answer (next week's deltas are commentary); the operator commands report it.
func Save(s State) bool {
	store, err := DefaultStore()
	if err != nil || store == nil {
		if err != nil {
			log.Warn(fmt.Sprintf("could not write state; next run will see everything as new: %s", err))
		}
		return false
	}
	ctx, cancel := context.WithTimeout(context.Background(), 60*time.Second)
	defer cancel()
	if err := store.Write(ctx, []byte(s.ToJSON())); err != nil {
		log.Warn(fmt.Sprintf("could not write state; next run will see everything as new: %s", err))
		return false
	}
	return true
}
