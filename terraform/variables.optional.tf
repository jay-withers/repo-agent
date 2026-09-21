variable "project_name" {
  description = "Project name included in every resource name. Lowercase only (container app jobs reject uppercase), and short enough that `caj-<project>-<env>-scan` fits the 32 characters container app jobs allow — the naming module truncates silently rather than failing. Changing this is also how to resolve a clash on the globally unique Key Vault name."
  type        = string
  default     = "repoagent"

  validation {
    condition     = can(regex("^[a-z][a-z0-9]{1,14}$", var.project_name))
    error_message = "project_name must be 2-15 lowercase alphanumeric characters, starting with a letter."
  }
}

# --- the shared platform ------------------------------------------------------
#
# Resolved by name rather than from the platform's Terraform state. These
# defaults match jay-withers/azure-container-apps at its own defaults; run
# `make outputs` there to confirm.

variable "platform_resource_group_name" {
  description = "Resource group holding the shared Container Apps environment."
  type        = string
  default     = "rg-platform-dev"
}

variable "platform_environment_name" {
  description = "Name of the shared Container Apps environment this job runs on."
  type        = string
  default     = "cae-platform-dev"
}

variable "platform_app_insights_name" {
  description = "Name of the shared Application Insights instance the job reports telemetry to."
  type        = string
  default     = "appi-platform-dev"
}

# --- this project -------------------------------------------------------------

variable "tags" {
  description = "Tags applied to all resources, merged with (and taking precedence over) the default tags (`environment`, `managed-by`)."
  type        = map(string)
  default     = {}
}

variable "key_vault_administrator_object_ids" {
  description = "Additional Entra object IDs granted `Key Vault Secrets Officer`, so they can populate secret values. Whoever runs `terraform apply` gets this automatically."
  type        = list(string)
  default     = []
}

variable "image_registry" {
  description = "Registry and repository prefix the image is pulled from. A public package on ghcr.io deliberately: a private one would need a `registry` block and a Key Vault-backed pull secret on the job, and there is no Azure Container Registry because ACR Basic is a flat monthly charge with no consumption tier."
  type        = string
  default     = "ghcr.io/jay-withers/repo-agent"
}

variable "image_tag" {
  description = "Image tag seeding the job's **first** revision only. After that the container's image and env are under `lifecycle.ignore_changes` and `make deploy` owns them, so a plan against an existing job reports no change here even when the running image has moved on. Don't read this as the deployed version; check `az containerapp job show`."
  type        = string
  # Must name a tag that actually exists in the registry. cd-tag's first
  # release on a repo with no prior tags is v0.0.1, not v0.1.0 — and a tag that
  # does not exist fails at revision start-up on the image pull, long after both
  # plan and apply have reported success.
  default = "v0.0.1"

  validation {
    # A moving tag deploys nothing: Container Apps creates a revision only when
    # the template changes, so re-pushing `latest` reports success and changes
    # nothing at all.
    condition     = !contains(["latest", "main", "unset"], var.image_tag)
    error_message = "image_tag must be an immutable tag, not latest/main/unset."
  }
}

variable "scan_cron_expression" {
  description = "When the scan runs, in UTC. Five fields, no seconds field, so the wall-clock time shifts with British Summer Time. Sunday evening by default, so the digest is waiting on Monday morning but is measured after a full week of Renovate merges rather than an hour into Monday's burst."
  type        = string
  default     = "0 18 * * 0"
}
