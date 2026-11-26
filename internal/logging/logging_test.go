package logging

import (
	"bytes"
	"log/slog"
	"strings"
	"testing"
)

func TestHandlerWritesPythonsFormat(t *testing.T) {
	var out bytes.Buffer
	restore := slog.Default()
	t.Cleanup(func() { slog.SetDefault(restore) })
	slog.SetDefault(slog.New(NewHandler(&out, "warning", "repoagent").WithAttrs([]slog.Attr{slog.String("k", "v")}).WithGroup("g")))

	log := Named("repoagent.fetch")
	log.Info("hidden")
	log.Debug("hidden too")
	log.Warnf("shown %d", 1)
	log.Errorf("and %s", "this")
	slog.Warn("unnamed", "n", 2)
	lines := strings.Split(strings.TrimSuffix(out.String(), "\n"), "\n")
	if len(lines) != 3 || strings.Contains(out.String(), "hidden") {
		t.Fatalf("got %q", out.String())
	}
	if !strings.HasSuffix(lines[0], " WARNING repoagent.fetch shown 1 k=v") || lines[0][19] != ',' ||
		!strings.HasSuffix(lines[1], " ERROR   repoagent.fetch and this k=v") ||
		!strings.HasSuffix(lines[2], " WARNING repoagent unnamed k=v n=2") {
		t.Errorf("got %q", lines)
	}
	Named("x").Infof("%d", 1)
	Named("x").Error("e")
}

func TestLevels(t *testing.T) {
	for level, name := range map[string]string{"DEBUG": "DEBUG", "INFO": "INFO", "WARN": "WARNING", "ERROR": "ERROR", "CRITICAL": "CRITICAL", "bogus": "INFO"} {
		if got := LevelName(ParseLevel(level)); got != name {
			t.Errorf("%s: %s", level, got)
		}
	}
}
