package checks

import (
	"fmt"
	"regexp"
	"strings"

	"github.com/jay-withers/repo-agent/internal/model"
	"github.com/jay-withers/repo-agent/internal/pyfmt"
)

// The root Dockerfile, read as text. Only Dockerfile at the root is fetched;
// a repository keeping one elsewhere is silent here.
var (
	fromRe    = regexp.MustCompile(`(?i)^\s*FROM\s+(?:--\S+\s+)*(\S+)(?:\s+AS\s+(\S+))?`)
	userRe    = regexp.MustCompile(`(?i)^\s*USER\s+(\S+)`)
	rootUsers = map[string]bool{"root": true, "0": true, "0:0": true, "root:root": true}
)

func dockerfileURL(s model.RepoSnapshot) string {
	return fmt.Sprintf("%s/blob/%s/Dockerfile", s.URL, s.DefaultBranch)
}

// UnpinnedBaseImage: base images by tag rather than digest. Stage aliases,
// scratch and $ARG-templated images have nothing to pin.
func UnpinnedBaseImage(s model.RepoSnapshot) []model.Finding {
	if deref(s.Dockerfile) == "" {
		return nil
	}
	stages := map[string]bool{}
	var unpinned []string
	for _, line := range pyfmt.SplitLines(*s.Dockerfile) {
		m := fromRe.FindStringSubmatch(line)
		if m == nil {
			continue
		}
		image, alias := m[1], m[2]
		if alias != "" {
			stages[strings.ToLower(alias)] = true
		}
		if stages[strings.ToLower(image)] || image == "scratch" || strings.Contains(image, "$") {
			continue
		}
		if !strings.Contains(image, "@sha256:") {
			unpinned = append(unpinned, image)
		}
	}
	if len(unpinned) == 0 {
		return nil
	}
	return finding(s, "container.unpinned_base_image", "medium",
		fmt.Sprintf("%d base image(s) not pinned to a digest", len(unpinned)),
		strings.Join(unpinned, ", ")+". A tag can move under the next build. Pin as "+
			"`image:tag@sha256:...` and let Renovate keep the digest current.",
		dockerfileURL(s), nil)
}

// RunsAsRoot: the final stage, the one that ships, runs as root. No USER at
// all means root.
func RunsAsRoot(s model.RepoSnapshot) []model.Finding {
	if deref(s.Dockerfile) == "" {
		return nil
	}
	var user *string
	seenFrom := false
	for _, line := range pyfmt.SplitLines(*s.Dockerfile) {
		if fromRe.MatchString(line) {
			seenFrom = true
			user = nil
		} else if m := userRe.FindStringSubmatch(line); m != nil {
			u := m[1]
			user = &u
		}
	}
	if !seenFrom || (user != nil && !rootUsers[strings.ToLower(*user)]) {
		return nil
	}
	lead := "The final stage sets no `USER`"
	if user != nil {
		lead = "`USER " + *user + "`"
	}
	return finding(s, "container.runs_as_root", "medium", "container runs as root",
		lead+", so the process runs as root, and a runtime enforcing `runAsNonRoot` "+
			"refuses it. Add a numeric `USER`, as this estate's template does.",
		dockerfileURL(s), nil)
}
