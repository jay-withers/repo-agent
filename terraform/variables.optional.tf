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
  description = "Entra object IDs granted `Key Vault Secrets Officer` (to populate secret values) and `Storage Blob Data Contributor` on the state container. Listed explicitly rather than taken from whoever runs `terraform apply`, because cd-terraform applies as the pipeline identity."
  type        = list(string)
  default     = []
}

variable "scan_cron_expression" {
  description = "When the scan runs, in UTC. Five fields, no seconds field, so the wall-clock time shifts with British Summer Time. Sunday evening by default, so the digest is waiting on Monday morning but is measured after a full week of Renovate merges rather than an hour into Monday's burst."
  type        = string
  default     = "0 18 * * 0"
}
