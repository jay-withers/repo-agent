package checks

import (
	"fmt"
	"slices"
	"strings"

	"github.com/jay-withers/repo-agent/internal/model"
	"github.com/jay-withers/repo-agent/internal/pyfmt"
)

// The labels jay-withers/workflows puts on the issues it opens. **A
// contract**: rename one there and these checks go silent.
const (
	TerraformPendingLabel = "terraform-pending"
	DeployPendingLabel    = "deploy-pending"
	DeployFailedLabel     = "deploy-failed"
	BuildFailedLabel      = "build-failed"
)

func labelled(s model.RepoSnapshot, label string) []model.Issue {
	if s.OpenIssues == nil {
		return nil
	}
	var out []model.Issue
	for _, issue := range *s.OpenIssues {
		if slices.Contains(issue.Labels, label) {
			out = append(out, issue)
		}
	}
	return out
}

func namedIssues(issues []model.Issue) string {
	parts := make([]string, len(issues))
	for i, issue := range issues {
		parts[i] = fmt.Sprintf("#%d %s", issue.Number, pyfmt.Repr(issue.Title))
	}
	return strings.Join(parts, ", ")
}

func labelURL(s model.RepoSnapshot, label string) string {
	return s.URL + "/issues?q=is%3Aissue+is%3Aopen+label%3A" + label
}

func oldestIssue(issues []model.Issue) *int {
	var oldest *int
	for _, i := range issues {
		if age := model.AgeDays(i.CreatedAt); age != nil && (oldest == nil || *age > *oldest) {
			oldest = age
		}
	}
	return oldest
}

// DeployFailed: deploys or applies on main that failed and have not
// succeeded since.
func DeployFailed(s model.RepoSnapshot) []model.Finding {
	issues := labelled(s, DeployFailedLabel)
	if len(issues) == 0 {
		return nil
	}
	return finding(s, "delivery.deploy_failed", "high", fmt.Sprintf("%d failed deploy(s) not yet fixed", len(issues)),
		namedIssues(issues)+". Each issue links the failed run. Fix the cause and re-run "+
			"it; the next successful run closes the issue.",
		labelURL(s, DeployFailedLabel), oldestIssue(issues))
}

// BuildFailed: scheduled builds of main that failed. Until main builds,
// every Renovate PR's container-build fails the same way.
func BuildFailed(s model.RepoSnapshot) []model.Finding {
	issues := labelled(s, BuildFailedLabel)
	if len(issues) == 0 {
		return nil
	}
	return finding(s, "delivery.build_failed", "high", "Image build on main is failing",
		namedIssues(issues)+". The issue links the failed run: the image no longer builds, "+
			"or Trivy found a fixable CRITICAL/HIGH vulnerability. Fix it on main; every "+
			"Renovate PR fails container-build until then. The next green build closes it.",
		labelURL(s, BuildFailedLabel), oldestIssue(issues))
}

// TerraformPending: Terraform merged to main and waiting to be applied.
func TerraformPending(s model.RepoSnapshot) []model.Finding {
	issues := labelled(s, TerraformPendingLabel)
	if len(issues) == 0 {
		return nil
	}
	return finding(s, "delivery.terraform_pending", "medium", "Terraform changes waiting to be applied",
		namedIssues(issues)+". Review the plan the issue links, then run cd-terraform from "+
			"the Actions tab to apply; the issue closes itself once nothing is left to apply.",
		labelURL(s, TerraformPendingLabel), oldestIssue(issues))
}

// DeployPending: releases held from deploying until someone runs cd-publish.
func DeployPending(s model.RepoSnapshot) []model.Finding {
	issues := labelled(s, DeployPendingLabel)
	if len(issues) == 0 {
		return nil
	}
	return finding(s, "delivery.deploy_pending", "medium", "Releases waiting to be deployed",
		namedIssues(issues)+". Each issue names the tag and why it was held. Run cd-publish "+
			"from the Actions tab with that tag; the issue closes itself once it deploys.",
		labelURL(s, DeployPendingLabel), oldestIssue(issues))
}
