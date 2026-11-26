// Package llm is triage: asking DeepSeek to order and explain what the
// checks already found.
//
// **The model never establishes a fact.** Three defences hold that line:
// the reply is validated strictly (a malformed reply is an error, not prose
// in an email); returned finding ids are intersected with the ids sent; and
// any finding the model omits is appended in its original order. The set
// that goes in is the set that comes out.
//
// What leaves the tenancy: repository names, the findings, and clipped text
// of each flagged repository's Renovate config, README and Dockerfile.
// Workflow names, never their contents. DeepSeek is China-hosted and may
// train on inputs, so the step is off unless DEEPSEEK-API-KEY is set.
package llm

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"strconv"
	"strings"
	"time"

	"github.com/jay-withers/repo-agent/internal/fetch"
	"github.com/jay-withers/repo-agent/internal/jv"
	"github.com/jay-withers/repo-agent/internal/logging"
	"github.com/jay-withers/repo-agent/internal/model"
	"github.com/jay-withers/repo-agent/internal/pyfmt"
	"github.com/jay-withers/repo-agent/internal/settings"
)

var log = logging.Named("repoagent.llm")

// PromptFileBudget is each file's ceiling in the prompt, in characters.
var PromptFileBudget = map[string]int{"renovate": 2000, "readme": 1200, "dockerfile": 1200}

// MaxOutputTokens leaves headroom for deepseek-flash's reasoning, which
// counts against it and bills as output.
const MaxOutputTokens = 4000

// Temperature is low, so priorities do not reshuffle weekly for no reason.
const Temperature = 0.2

// Prices are USD per million tokens for deepseek-flash, off-peak and peak.
// A hand-kept copy of DeepSeek's price list (checked 2026-09-19), so the
// cost is an estimate; the balance is the number to trust.
var Prices = map[string][2]float64{
	"cache_hit":  {0.003, 0.006},
	"cache_miss": {0.15, 0.3},
	"output":     {0.6, 1.2},
}

// peakWindowsUTC are DeepSeek's peak hours, Monday to Friday.
var peakWindowsUTC = [][2]int{{1, 4}, {6, 10}}

// LowBalanceUSD is where the next digest is at real risk of a 402.
const LowBalanceUSD = 1.0

// MaxSuggestions is enforced here, not trusted to the prompt.
const MaxSuggestions = 4

// SystemPrompt is the Python's _SYSTEM_PROMPT, verbatim.
const SystemPrompt = `You are triaging the output of an automated GitHub repository scanner.

The findings below were produced by deterministic code, not by you. They are ` +
	`facts. Your job is to decide what matters most and to explain why, in the ` +
	`context of the whole estate.

Rules, all of them absolute:

1. Do not invent findings. Use only the ids you are given.
2. Do not restate counts, totals or lists. The email already renders a full ` +
	`table of every finding above your text, and repeating it wastes the only part ` +
	`of the message a human reads.
3. Do not describe what the scanner did. Describe what the maintainer should do.
4. Your summary is at most three sentences. Lead with the single most urgent ` +
	`thing, name the repository, and say what action it needs.
5. If nothing is genuinely urgent, say so plainly in one sentence rather than ` +
	`manufacturing concern.

You may also suggest improvements the scanner does not check for. These are ` +
	`opinions, are rendered separately from the findings, and are not counted ` +
	`anywhere. Rules for them:

6. Never restate a finding as a suggestion. The findings are already reported.
7. Never suggest something the scanner already checks: Renovate configuration ` +
	`and PR backlogs, README, LICENCE, description, CI presence, repository ` +
	`staleness, catalogue membership, Terraform lock platforms, action pinning, ` +
	`runner versions, shared Renovate preset, required checks nothing reports, ` +
	`pull request checks the catalogue does not require, workflow token permissions, ` +
	`pull_request_target misuse, Dockerfile base image pinning and USER, abandoned ` +
	`pull requests, stale branches, open CodeQL code scanning alerts.
8. Be specific to what you were shown. "Add tests" is worthless; "the Dockerfile ` +
	`installs build tools into the runtime stage" is worth reading.
9. At most four. Fewer is better. An empty list is a fine answer.

Respond with JSON only, matching this shape:

{"summary": "<at most three sentences>", "order": ["<finding id>", ...], ` +
	`"themes": ["<short phrase>", ...], ` +
	`"suggestions": [{"repo": "<owner/name or empty for the estate>", ` +
	`"text": "<one sentence>"}, ...]}

` + "`order`" + ` lists every id you were given, most important first. ` + "`themes`" + ` is at ` +
	`most three short phrases naming patterns across repositories, or an empty list.
`

// Reply is the model's answer, validated.
type Reply struct {
	Summary     string
	Order       []string
	Themes      []string
	Suggestions []model.Suggestion
}

// errValidation marks a reply that does not match the schema.
var errValidation = errors.New("triage reply failed validation")

func invalid(format string, args ...any) error {
	return fmt.Errorf("%w: %s", errValidation, fmt.Sprintf(format, args...))
}

// ParseReply validates the reply as the Python's pydantic Triage model did:
// every field optional, every present field the right type, summary at most
// 2,000 characters, at most three themes, suggestion text at most 400 and
// repo at most 200. Unknown fields are ignored.
func ParseReply(content string) (Reply, error) {
	var raw map[string]json.RawMessage
	if err := json.Unmarshal([]byte(content), &raw); err != nil || raw == nil {
		return Reply{}, invalid("not a JSON object")
	}
	var r Reply
	if v, ok := raw["summary"]; ok {
		if err := json.Unmarshal(v, &r.Summary); err != nil || string(v) == "null" {
			return Reply{}, invalid("summary is not a string")
		}
		if pyfmt.Len(r.Summary) > 2000 {
			return Reply{}, invalid("summary is over 2000 characters")
		}
	}
	strList := func(key string) ([]string, error) {
		v, ok := raw[key]
		if !ok {
			return nil, nil
		}
		var items []*string
		if err := json.Unmarshal(v, &items); err != nil || string(v) == "null" {
			return nil, invalid("%s is not a list of strings", key)
		}
		out := make([]string, len(items))
		for i, item := range items {
			if item == nil {
				return nil, invalid("%s holds a null", key)
			}
			out[i] = *item
		}
		return out, nil
	}
	var err error
	if r.Order, err = strList("order"); err != nil {
		return Reply{}, err
	}
	if r.Themes, err = strList("themes"); err != nil {
		return Reply{}, err
	}
	if len(r.Themes) > 3 {
		return Reply{}, invalid("more than three themes")
	}
	if v, ok := raw["suggestions"]; ok {
		var items []map[string]json.RawMessage
		if err := json.Unmarshal(v, &items); err != nil || string(v) == "null" {
			return Reply{}, invalid("suggestions is not a list of objects")
		}
		for _, item := range items {
			if item == nil {
				return Reply{}, invalid("a suggestion is not an object")
			}
			var s model.Suggestion
			for key, limit := range map[string]int{"text": 400, "repo": 200} {
				field, ok := item[key]
				if !ok {
					continue
				}
				var value string
				if err := json.Unmarshal(field, &value); err != nil || string(field) == "null" {
					return Reply{}, invalid("suggestion %s is not a string", key)
				}
				if pyfmt.Len(value) > limit {
					return Reply{}, invalid("suggestion %s is over %d characters", key, limit)
				}
				if key == "text" {
					s.Text = value
				} else {
					s.Repo = value
				}
			}
			r.Suggestions = append(r.Suggestions, s)
		}
	}
	return r, nil
}

// Triage reorders findings and produces a summary, or passes them through
// unchanged. A failed call costs the digest its commentary, never its
// findings. The one error is a key that cannot be resolved (an absent key is
// fine; a broken credential is not), which the Python raised too.
func Triage(client *http.Client, findings []model.Finding, repos []model.RepoSnapshot) (model.TriageOutcome, error) {
	if len(findings) == 0 {
		return model.TriageOutcome{Findings: findings}, nil
	}
	key, err := settings.OptionalSecret("DEEPSEEK-API-KEY")
	if err != nil {
		return model.TriageOutcome{}, err
	}
	if key == "" {
		log.Info("no DEEPSEEK-API-KEY, skipping triage")
		return model.TriageOutcome{Findings: findings}, nil
	}
	reply, usage, err := ask(client, key, UserPrompt(findings, repos))
	if err != nil {
		log.Warn(fmt.Sprintf("triage failed, reporting findings unordered: %s", err))
		return model.TriageOutcome{Findings: findings}, nil
	}
	usage = withBalance(client, usage, key)
	return model.TriageOutcome{
		Findings:    apply(findings, reply),
		Summary:     strings.TrimSpace(reply.Summary),
		Themes:      reply.Themes,
		Usage:       &usage,
		Suggestions: suggestions(reply, repos),
	}, nil
}

func suggestions(reply Reply, repos []model.RepoSnapshot) []model.Suggestion {
	known := map[string]bool{}
	for _, r := range repos {
		known[r.FullName] = true
	}
	var kept []model.Suggestion
	for _, item := range reply.Suggestions {
		text := strings.TrimSpace(item.Text)
		if text == "" {
			continue
		}
		repo := strings.TrimSpace(item.Repo)
		if repo != "" && !known[repo] {
			log.Warn("dropping suggestion about unknown repository " + pyfmt.Repr(repo))
			continue
		}
		kept = append(kept, model.Suggestion{Text: text, Repo: repo})
		if len(kept) == MaxSuggestions {
			break
		}
	}
	return kept
}

func ask(client *http.Client, key, prompt string) (Reply, model.TriageUsage, error) {
	cfg := settings.Get()
	raw, err := fetch.PostJSON(client, cfg.DeepSeekAPIURL+"/chat/completions", map[string]any{
		"model": cfg.DeepSeekModel,
		"messages": []map[string]string{
			{"role": "system", "content": SystemPrompt},
			{"role": "user", "content": prompt},
		},
		// JSON mode: without it the reply arrives fenced one time in five.
		"response_format": map[string]string{"type": "json_object"},
		"max_tokens":      MaxOutputTokens,
		"temperature":     Temperature,
	}, map[string]string{"Authorization": "Bearer " + key, "Content-Type": "application/json"})
	if err != nil {
		return Reply{}, model.TriageUsage{}, err
	}
	payload := jv.Obj(raw)
	choices := jv.List(payload["choices"])
	if len(choices) == 0 {
		return Reply{}, model.TriageUsage{}, errors.New("reply has no choices")
	}
	choice := jv.Obj(choices[0])
	// Before parsing: a truncated reply is invalid JSON whose error names a
	// missing brace rather than the cap that caused it.
	if jv.Str(choice, "finish_reason") == "length" {
		return Reply{}, model.TriageUsage{}, fmt.Errorf(
			"reply hit the %d-token cap and was truncated; raise MaxOutputTokens or ask for less", MaxOutputTokens)
	}
	content, ok := jv.Obj(choice["message"])["content"].(string)
	if !ok {
		return Reply{}, model.TriageUsage{}, errors.New("reply has no message content")
	}
	served := jv.Str(payload, "model")
	if served == "" {
		served = cfg.DeepSeekModel
	}
	usage := Usage(jv.Obj(payload["usage"]), served, model.Now())
	peak := ""
	if usage.Peak {
		peak = " at peak rates"
	}
	log.Info(fmt.Sprintf("triage used %d prompt (%d cached) / %d completion tokens, est. $%.4f%s",
		usage.PromptTokens(), usage.CacheHitTokens, usage.CompletionTokens, *usage.CostUSD, peak))
	reply, err := ParseReply(content)
	return reply, usage, err
}

// Usage is the token counts and an estimated cost. Without DeepSeek's
// cache fields everything counts as a miss, which over-estimates.
func Usage(raw map[string]any, served string, now time.Time) model.TriageUsage {
	prompt := jv.IntOr(raw, "prompt_tokens", 0)
	hit := jv.IntOr(raw, "prompt_cache_hit_tokens", 0)
	miss := jv.IntOr(raw, "prompt_cache_miss_tokens", prompt-hit)
	completion := jv.IntOr(raw, "completion_tokens", 0)
	peak := IsPeak(now)
	i := 0
	if peak {
		i = 1
	}
	cost := (float64(hit)*Prices["cache_hit"][i] + float64(miss)*Prices["cache_miss"][i] +
		float64(completion)*Prices["output"][i]) / 1_000_000
	return model.TriageUsage{
		Model: served, CacheHitTokens: hit, CacheMissTokens: miss,
		CompletionTokens: completion, Peak: peak, CostUSD: &cost,
	}
}

// IsPeak is whether DeepSeek's peak multiplier applies. Weekends are
// off-peak; Chinese public holidays are too and are not modelled, which
// over-estimates.
func IsPeak(t time.Time) bool {
	t = t.UTC()
	if t.Weekday() == time.Saturday || t.Weekday() == time.Sunday {
		return false
	}
	for _, w := range peakWindowsUTC {
		if w[0] <= t.Hour() && t.Hour() < w[1] {
			return true
		}
	}
	return false
}

// withBalance attaches the account's remaining USD balance, or leaves it
// absent. Never fails.
func withBalance(client *http.Client, usage model.TriageUsage, key string) model.TriageUsage {
	raw, err := fetch.GetJSON(client, settings.Get().DeepSeekAPIURL+"/user/balance",
		map[string]string{"Authorization": "Bearer " + key}, nil)
	if err != nil {
		log.Warn(fmt.Sprintf("could not read DeepSeek balance: %s", err))
		return usage
	}
	for _, info := range jv.List(jv.Obj(raw)["balance_infos"]) {
		entry := jv.Obj(info)
		if jv.Str(entry, "currency") != "USD" {
			continue
		}
		balance := ""
		switch v := entry["total_balance"].(type) {
		case string:
			balance = v
		case json.Number:
			balance = pyNumber(v)
		}
		if f, err := strconv.ParseFloat(balance, 64); err == nil && f < LowBalanceUSD {
			log.Warn(fmt.Sprintf("DeepSeek balance is $%s — triage will stop when it runs out", balance))
		}
		usage.BalanceUSD = &balance
		return usage
	}
	return usage
}

// pyNumber is Python's str() of a decoded JSON number: an integer as it
// was written, a float as its shortest repr, with ".0" when whole.
func pyNumber(n json.Number) string {
	text := n.String()
	if !strings.ContainsAny(text, ".eE") {
		return text
	}
	f, err := n.Float64()
	if err != nil {
		return text
	}
	// repr(float): scientific below 1e-4 and from 1e16, fixed otherwise.
	sci := strconv.FormatFloat(f, 'e', -1, 64)
	mant, exp, _ := strings.Cut(sci, "e")
	e, _ := strconv.Atoi(exp)
	if f != 0 && (e < -4 || e >= 16) {
		sign, digits := "+", strconv.Itoa(e)
		if e < 0 {
			sign, digits = "-", strconv.Itoa(-e)
		}
		if len(digits) < 2 {
			digits = "0" + digits
		}
		return mant + "e" + sign + digits
	}
	out := strconv.FormatFloat(f, 'f', -1, 64)
	if !strings.Contains(out, ".") {
		out += ".0"
	}
	return out
}

// apply reorders findings by the reply, keeping every one.
func apply(findings []model.Finding, reply Reply) []model.Finding {
	byID := map[string]model.Finding{}
	for _, f := range findings {
		byID[f.ID()] = f
	}
	seen := map[string]bool{}
	var ordered []model.Finding
	for _, id := range reply.Order {
		if f, ok := byID[id]; ok && !seen[id] {
			ordered = append(ordered, f)
			seen[id] = true
		}
	}
	var dropped []model.Finding
	for _, f := range findings {
		if !seen[f.ID()] {
			dropped = append(dropped, f)
		}
	}
	if len(dropped) > 0 {
		log.Warn(fmt.Sprintf("triage omitted %d finding(s); appending them", len(dropped)))
	}
	return append(ordered, dropped...)
}

// UserPrompt is the findings and just enough context to judge them by,
// as compact ensure_ascii JSON: the bytes the Python sent.
func UserPrompt(findings []model.Finding, repos []model.RepoSnapshot) string {
	byName := map[string]model.RepoSnapshot{}
	for _, r := range repos {
		byName[r.FullName] = r
	}
	names := map[string]bool{}
	for _, f := range findings {
		names[f.Repo] = true
	}
	sorted := make([]string, 0, len(names))
	for n := range names {
		sorted = append(sorted, n)
	}
	sort.Strings(sorted)

	context := []any{}
	for _, name := range sorted {
		repo, ok := byName[name]
		if !ok {
			continue
		}
		context = append(context, pyfmt.Object{
			{Key: "repo", Value: name},
			{Key: "description", Value: repo.Description},
			{Key: "private", Value: repo.Private},
			{Key: "workflows", Value: append([]string{}, repo.WorkflowNames()...)},
			{Key: "open_renovate_prs", Value: len(repo.RenovatePRs())},
			{Key: "last_release", Value: repo.LastRelease},
			{Key: "renovate_config", Value: clip(repo.RenovateConfig, "renovate")},
			{Key: "readme", Value: clip(repo.Readme, "readme")},
			{Key: "dockerfile", Value: clip(repo.Dockerfile, "dockerfile")},
		})
	}
	items := make([]any, len(findings))
	for i, f := range findings {
		items[i] = pyfmt.Object{
			{Key: "id", Value: f.ID()},
			{Key: "repo", Value: f.Repo},
			{Key: "check", Value: f.Check},
			{Key: "severity", Value: f.Severity},
			{Key: "title", Value: f.Title},
			{Key: "detail", Value: f.Detail},
			{Key: "age_days", Value: f.AgeDays},
		}
	}
	return pyfmt.JSON(pyfmt.Object{
		{Key: "findings", Value: items},
		{Key: "repositories", Value: context},
	}, 0)
}

func clip(text *string, budget string) *string {
	if text == nil || *text == "" {
		return nil
	}
	if cut, truncated := pyfmt.Truncate(*text, PromptFileBudget[budget]); truncated {
		return model.Ptr(cut + "\n… [truncated]")
	}
	return text
}
