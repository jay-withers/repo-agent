// Command repoagent: `repoagent scan|render|state|suppress|unsuppress|selfcheck`.
//
// `scan` and `render` are the workload; `state`, `suppress` and
// `unsuppress` are operator commands for the state document; `selfcheck`
// is what `make smoke` runs after a deploy. One image, one ENTRYPOINT
// (this binary), and Terraform sets only `args`: **never a `command`**, so
// the executable's name has one source of truth (see docs/design.md).
package main

import (
	"errors"
	"fmt"
	"io"
	"log/slog"
	"os"
	"os/signal"
	"strings"
	"syscall"

	"github.com/jay-withers/repo-agent/internal/digest"
	"github.com/jay-withers/repo-agent/internal/fetch"
	"github.com/jay-withers/repo-agent/internal/github"
	"github.com/jay-withers/repo-agent/internal/logging"
	"github.com/jay-withers/repo-agent/internal/model"
	"github.com/jay-withers/repo-agent/internal/pyfmt"
	"github.com/jay-withers/repo-agent/internal/scan"
	"github.com/jay-withers/repo-agent/internal/settings"
	"github.com/jay-withers/repo-agent/internal/state"
)

// The Python's logger names: repoagent.cli for a failed run, repoagent.state
// for the operator commands, repoagent.selfcheck for selfcheck.
var (
	cliLog   = logging.Named("repoagent.cli")
	stateLog = logging.Named("repoagent.state")
	selfLog  = logging.Named("repoagent.selfcheck")
)

func main() {
	os.Exit(run(os.Args[1:], os.Stdout, os.Stderr))
}

const usage = `usage: repoagent {scan,render,state,suppress,unsuppress,selfcheck} ...

  scan        scan every repository and email the digest
  render      scan and print the digest, sending nothing
  state       print what the scan remembers between runs
  suppress    stop reporting one finding, with a reason
              repoagent suppress FINDING_ID --reason REASON [--until YYYY-MM-DD]
  unsuppress  start reporting a finding again
              repoagent unsuppress FINDING_ID
  selfcheck   check settings, secrets and storage as a job would, doing nothing
`

// args are one subcommand's parsed arguments.
type args struct {
	command   string
	findingID string
	reason    *string
	until     *string
}

// parse reads argv as argparse did: options may come before or after the
// positional, `--opt value` and `--opt=value` both work. Usage errors exit 2.
func parse(argv []string) (args, error) {
	if len(argv) == 0 {
		return args{}, errors.New("the following arguments are required: command")
	}
	a := args{command: argv[0]}
	switch a.command {
	case "scan", "render", "state", "selfcheck", "suppress", "unsuppress":
	case "-h", "--help":
		return args{command: "help"}, nil
	default:
		return args{}, fmt.Errorf("argument command: invalid choice: %s", pyfmt.Repr(a.command))
	}
	var positional []string
	rest := argv[1:]
	for i := 0; i < len(rest); i++ {
		arg := rest[i]
		if arg == "-h" || arg == "--help" {
			return args{command: "help"}, nil
		}
		if !strings.HasPrefix(arg, "--") || a.command != "suppress" {
			if strings.HasPrefix(arg, "-") {
				return args{}, fmt.Errorf("unrecognized arguments: %s", arg)
			}
			positional = append(positional, arg)
			continue
		}
		name, value, inline := strings.Cut(arg[2:], "=")
		if name != "reason" && name != "until" {
			return args{}, fmt.Errorf("unrecognized arguments: %s", arg)
		}
		if !inline {
			if i+1 >= len(rest) {
				return args{}, fmt.Errorf("argument --%s: expected one argument", name)
			}
			i++
			value = rest[i]
		}
		if name == "reason" {
			a.reason = &value
		} else {
			a.until = &value
		}
	}
	switch a.command {
	case "suppress", "unsuppress":
		if len(positional) != 1 {
			return args{}, errors.New("the following arguments are required: finding_id")
		}
		a.findingID = positional[0]
		// Required: a suppression without a reason is indistinguishable in
		// six months from a bug.
		if a.command == "suppress" && a.reason == nil {
			return args{}, errors.New("the following arguments are required: --reason")
		}
	default:
		if len(positional) > 0 {
			return args{}, fmt.Errorf("unrecognized arguments: %s", strings.Join(positional, " "))
		}
	}
	return a, nil
}

func run(argv []string, stdout, stderr io.Writer) int {
	a, err := parse(argv)
	if err != nil {
		_, _ = fmt.Fprint(stderr, usage)
		_, _ = fmt.Fprintf(stderr, "repoagent: error: %s\n", err)
		return 2
	}
	if a.command == "help" {
		_, _ = fmt.Fprint(stdout, usage)
		return 0
	}

	slog.SetDefault(slog.New(logging.NewHandler(stdout, settings.Get().LogLevel, "repoagent")))

	// Container Apps sends SIGTERM before SIGKILL at the replica timeout.
	// Exit non-zero so the execution is marked failed, which the platform's
	// job-failure alert watches for.
	terminated := make(chan os.Signal, 1)
	signal.Notify(terminated, syscall.SIGTERM)
	defer signal.Stop(terminated)
	go func() {
		if sig, ok := <-terminated; ok {
			cliLog.Errorf("repoagent %s failed: terminated by signal %d", a.command, sig)
			os.Exit(1)
		}
	}()

	switch a.command {
	case "state", "suppress", "unsuppress":
		return stateCommand(a, stdout)
	case "selfcheck":
		return selfcheck()
	}

	result, err := scan.Run(fetch.NewClient(), a.command == "scan")
	if err != nil {
		cliLog.Errorf("repoagent %s failed: %s", a.command, err)
		return 1
	}
	if a.command == "render" {
		_, _ = fmt.Fprintln(stdout, digest.Text(result))
	}
	return 0
}

// stateCommand runs the operator commands, which **report failure**: a
// save that silently did nothing would leave someone believing they had
// suppressed a finding.
func stateCommand(a args, stdout io.Writer) int {
	store := state.Load()
	if a.command == "state" {
		_, _ = fmt.Fprintln(stdout, store.ToJSON())
		return 0
	}
	if a.command == "unsuppress" {
		if _, ok := store.Suppressed[a.findingID]; !ok {
			stateLog.Error(a.findingID + " is not suppressed")
			return 1
		}
		delete(store.Suppressed, a.findingID)
	} else {
		if a.until != nil {
			if _, ok := state.ParseDate(*a.until); !ok {
				stateLog.Errorf("--until %s is not a YYYY-MM-DD date", pyfmt.Repr(*a.until))
				return 1
			}
		}
		// Warn rather than refuse: a resolved finding may come back, and
		// pre-suppressing one is legitimate.
		if _, ok := store.Findings[a.findingID]; !ok {
			stateLog.Warn(a.findingID + " is not a finding this run knows about")
		}
		store.Suppressed[a.findingID] = state.Suppression{
			Reason: *a.reason,
			Until:  a.until,
			Added:  model.Now().Format("2006-01-02"),
		}
	}
	if !state.Save(store) {
		stateLog.Error("could not write the state document; nothing was changed")
		return 1
	}
	stateLog.Info(fmt.Sprintf("state updated: %s %sd", a.findingID, a.command))
	return 0
}

// selfSecrets are what the scan reads with Secret (required) and
// OptionalSecret. Add one here when the scan starts reading it.
var (
	selfSecrets         = []string{"GITHUB-APP-ID", "GITHUB-APP-PRIVATE-KEY", "RESEND-API-KEY"}
	selfOptionalSecrets = []string{"DIGEST-EMAIL-TO", "DEEPSEEK-API-KEY"}
)

type selfCheck struct {
	label string
	check func() error
}

func selfChecks() []selfCheck {
	checks := []selfCheck{{"settings", func() error { settings.Get(); return nil }}}
	for _, name := range selfSecrets {
		checks = append(checks, selfCheck{"secret " + name, func() error {
			v, err := settings.Secret(name)
			if err == nil && v == "" {
				err = errors.New("resolved to an empty value")
			}
			return err
		}})
	}
	for _, name := range selfOptionalSecrets {
		checks = append(checks, selfCheck{"secret " + name + " (optional)", func() error {
			_, err := settings.OptionalSecret(name)
			return err
		}})
	}
	return append(checks,
		// A real call: the App's key, its installation and GitHub all work.
		selfCheck{"github app token", func() error { _, err := github.Token(fetch.NewClient()); return err }},
		// Load degrades to empty on purpose, so this proves the call path.
		selfCheck{"storage", func() error { state.Load(); return nil }},
	)
}

// selfcheck proves a job's container could do a real run, and does
// nothing. Never logs a secret's value.
func selfcheck() int {
	failed := 0
	for _, c := range selfChecks() {
		if err := c.check(); err != nil {
			failed++
			msg, _ := pyfmt.Truncate(err.Error(), 300)
			selfLog.Error(fmt.Sprintf("selfcheck: FAIL %s: %s", c.label, msg))
		} else {
			selfLog.Info("selfcheck: ok " + c.label)
		}
	}
	if failed > 0 {
		selfLog.Error(fmt.Sprintf("selfcheck: %d check(s) failed", failed))
		return 1
	}
	selfLog.Info("selfcheck: all checks passed")
	return 0
}
