// Package logging writes the Python logging format the job has always
// written, one logger name per module:
//
//	2026-11-29 18:00:01,123 WARNING repoagent.fetch message
//
// Not slog's JSON or key=value forms, deliberately: observability's obsview
// groups error lines by masking quoted values, so every JSON line would mask
// to one signature. The names are the Python modules' `__name__`s, so an
// error's signature in obsview survives the cutover.
package logging

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"strings"
	"sync"
)

// nameKey carries a record's logger name to the Handler.
const nameKey = "logger"

// Logger logs under one name through whatever slog.Default is at the call.
type Logger struct{ name string }

// Named is the logger for one module, e.g. Named("repoagent.fetch").
func Named(name string) Logger { return Logger{name} }

// Debug logs at DEBUG.
func (l Logger) Debug(msg string) { slog.Default().Debug(msg, nameKey, l.name) }

// Info logs at INFO.
func (l Logger) Info(msg string) { slog.Default().Info(msg, nameKey, l.name) }

// Warn logs at WARNING.
func (l Logger) Warn(msg string) { slog.Default().Warn(msg, nameKey, l.name) }

// Error logs at ERROR.
func (l Logger) Error(msg string) { slog.Default().Error(msg, nameKey, l.name) }

// Infof, Warnf and Errorf format first.
func (l Logger) Infof(format string, args ...any) { l.Info(fmt.Sprintf(format, args...)) }

// Warnf is Warn with formatting.
func (l Logger) Warnf(format string, args ...any) { l.Warn(fmt.Sprintf(format, args...)) }

// Errorf is Error with formatting.
func (l Logger) Errorf(format string, args ...any) { l.Error(fmt.Sprintf(format, args...)) }

// Handler is the slog.Handler that writes the Python format.
type Handler struct {
	mu    *sync.Mutex
	w     io.Writer
	level slog.Level
	name  string
	attrs []slog.Attr
}

// NewHandler logs at `level` (Python's names: DEBUG, INFO, WARNING, ERROR,
// CRITICAL), naming a record with no logger `fallback`.
func NewHandler(w io.Writer, level, fallback string) *Handler {
	return &Handler{mu: &sync.Mutex{}, w: w, level: ParseLevel(level), name: fallback}
}

// ParseLevel reads a Python level name; anything unknown is INFO, as
// `getattr(logging, name, logging.INFO)` was.
func ParseLevel(s string) slog.Level {
	switch strings.ToUpper(s) {
	case "DEBUG":
		return slog.LevelDebug
	case "WARNING", "WARN":
		return slog.LevelWarn
	case "ERROR":
		return slog.LevelError
	case "CRITICAL":
		return slog.LevelError + 4
	}
	return slog.LevelInfo
}

// LevelName is Python's name for a level.
func LevelName(l slog.Level) string {
	switch {
	case l >= slog.LevelError+4:
		return "CRITICAL"
	case l >= slog.LevelError:
		return "ERROR"
	case l >= slog.LevelWarn:
		return "WARNING"
	case l >= slog.LevelInfo:
		return "INFO"
	}
	return "DEBUG"
}

// Enabled implements slog.Handler.
func (h *Handler) Enabled(_ context.Context, l slog.Level) bool { return l >= h.level }

// Handle implements slog.Handler.
func (h *Handler) Handle(_ context.Context, r slog.Record) error {
	name := h.name
	var extra []string
	collect := func(a slog.Attr) bool {
		if a.Key == nameKey {
			name = a.Value.String()
		} else {
			extra = append(extra, fmt.Sprintf("%s=%v", a.Key, a.Value))
		}
		return true
	}
	for _, a := range h.attrs {
		collect(a)
	}
	r.Attrs(collect)
	t := r.Time.UTC()
	var b strings.Builder
	fmt.Fprintf(&b, "%s,%03d %-7s %s %s", t.Format("2006-01-02 15:04:05"), t.Nanosecond()/1e6,
		LevelName(r.Level), name, r.Message)
	for _, e := range extra {
		b.WriteString(" " + e)
	}
	b.WriteByte('\n')
	h.mu.Lock()
	defer h.mu.Unlock()
	_, err := io.WriteString(h.w, b.String())
	return err
}

// WithAttrs implements slog.Handler.
func (h *Handler) WithAttrs(attrs []slog.Attr) slog.Handler {
	c := *h
	c.attrs = append(append([]slog.Attr(nil), h.attrs...), attrs...)
	return &c
}

// WithGroup implements slog.Handler; groups are flattened.
func (h *Handler) WithGroup(string) slog.Handler { return h }
