# This project's own infrastructure. The Container Apps environment it runs on
# is **not** here — it is shared, lives in jay-withers/azure-container-apps,
# and is resolved by name in data.tf.
#
# Everything this project owns is in its own resource group with its own Key
# Vault and its own identity, so a secret here is readable by nothing else on
# the platform.
module "naming" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver
  # (version below), not a git source — there's no commit hash to pin.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.project_name, var.environment]
}

# Its own instance so the job's name carries the workload, matching the
# convention the platform's job-failure alert expects
# (caj-<project>-<env>-<workload>).
module "naming_scan" {
  # checkov:skip=CKV_TF_1: Terraform Registry module pinned by semver.
  source  = "Azure/naming/azurerm"
  version = "~> 0.4"
  suffix  = [var.project_name, var.environment, "scan"]
}

# The resource group is github-repos' (`create_resource_groups`), which
# imported the one this config's first apply made, and grants the pipeline
# Contributor and conditioned RBAC Administrator on it. Dropped from this state
# without touching Azure; data.tf looks it up by the same name.
removed {
  from = azurerm_resource_group.this

  lifecycle {
    destroy = false
  }
}
