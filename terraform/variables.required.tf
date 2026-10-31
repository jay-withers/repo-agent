variable "environment" {
  description = "Deployment environment. Drives resource naming, and selects which shared platform environment this project deploys onto."
  type        = string

  validation {
    condition     = contains(["dev", "stg", "prd"], var.environment)
    error_message = "environment must be one of: dev, stg, prd."
  }
}

variable "key_vault_administrator_object_ids" {
  description = "Entra object IDs granted `Key Vault Secrets Officer` (to populate secret values) and `Storage Blob Data Contributor` on the state container. Listed explicitly rather than taken from whoever runs `terraform apply`, because cd-terraform applies as the pipeline identity. Not committed: CI reads it from the TF_VAR_KEY_VAULT_ADMINISTRATOR_OBJECT_IDS variable github-repos sets on the `dev` environment; a local plan needs TF_VAR_key_vault_administrator_object_ids exported."
  type        = list(string)
}
