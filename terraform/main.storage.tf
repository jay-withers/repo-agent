# Where the scan remembers what it saw last week.
#
# One small JSON document, read whole at the start of a run and written whole at
# the end. That access pattern is why this is a blob rather than a table: there
# is no query to serve, and a weekly job with `parallelism = 1` has no concurrent
# writer to arbitrate. Postgres would cost more per month than this project
# spends in a year, and market-agent's is not shared — see CLAUDE.md.
# tflint-ignore: azurerm_resources_missing_prevent_destroy
resource "azurerm_storage_account" "state" {
  # `prevent_destroy` is deliberately absent, for the same reason the Key Vault
  # turns purge protection off: this environment should be destroyable, and a
  # lifecycle block that blocks `terraform destroy` makes tearing dev down a
  # manual job. What is stored here is rebuildable — a lost state document costs
  # one week's deltas, not correctness.
  #
  # checkov:skip=CKV_AZURE_206: LRS on purpose. Geo-redundancy for a file that any
  #   subsequent run reconstructs is paying a monthly premium to protect one
  #   week of "is this finding new".
  # checkov:skip=CKV_AZURE_59: public network access stays enabled, for the same
  #   reason as the Key Vault — a scale-to-zero job on a Consumption-only shared
  #   environment has neither a VNet to peer nor a static egress IP to allow.
  # checkov:skip=CKV_AZURE_33: no queues are used, so queue logging has nothing
  #   to log.
  # checkov:skip=CKV2_AZURE_1: platform-managed keys. A CMK needs a second Key
  #   Vault key, rotation, and a managed identity grant on it, to protect one
  #   file of finding ids that are themselves hashes of public repository names.
  # checkov:skip=CKV2_AZURE_33: no private endpoint, as above.
  # checkov:skip=CKV2_AZURE_47: public access as above.
  name                = module.naming.storage_account.name
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location

  account_tier             = "Standard"
  account_kind             = "StorageV2"
  account_replication_type = "LRS"

  # The document is rebuilt from scratch by any run that loses it: a missing
  # state file costs one week's deltas, not correctness. Paying for
  # geo-redundancy to protect that would be silly.

  min_tls_version                 = "TLS1_2"
  https_traffic_only_enabled      = true
  allow_nested_items_to_be_public = false
  public_network_access           = "Enabled"

  # **Entra ID only.** Turning off shared keys means no connection string and no
  # SAS exists to leak, and the managed identity below is the only way in. It
  # also means `az storage blob` needs `--auth-mode login`.
  shared_access_key_enabled = false

  blob_properties {
    # The history behind the history. Each weekly write becomes a version, so a
    # state document corrupted by a bad deploy can be read back rather than
    # reconstructed, and "what did it think in October" is answerable.
    versioning_enabled = true

    delete_retention_policy {
      days = 30
    }
    container_delete_retention_policy {
      days = 30
    }
  }

  tags = local.tags
}

# tflint-ignore: azurerm_resources_missing_prevent_destroy
resource "azurerm_storage_container" "state" {
  # checkov:skip=CKV2_AZURE_21: no blob read logging. It would land in the shared
  #   Log Analytics workspace, whose `daily_quota_gb = 0.15` is split across every
  #   project on the platform — spent on recording that a weekly job read one file
  #   it already logs reading.
  name                  = "state"
  storage_account_id    = azurerm_storage_account.state.id
  container_access_type = "private"
}

# The job reads and writes its own state document.
#
# Scoped to the container rather than the account: the identity has no reason to
# reach any other container, and this one is the only thing in here.
#
# The scope is assembled by hand because `azurerm_storage_container` exports no
# Resource Manager id — its `id` is the data-plane URL, which a role assignment
# will not accept.
resource "azurerm_role_assignment" "identity_state_contributor" {
  scope                = local.state_container_scope
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.this.principal_id
  # Stated explicitly: without it the provider looks the principal up in the
  # directory, which fails intermittently on an identity created moments ago.
  principal_type = "ServicePrincipal"
}

# Whoever applies can read and edit the state document by hand — which is what
# `repoagent suppress` does, running locally against the real blob.
resource "azurerm_role_assignment" "deployer_state_contributor" {
  for_each = toset(concat(
    [data.azurerm_client_config.current.object_id],
    var.key_vault_administrator_object_ids,
  ))

  scope                = local.state_container_scope
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = each.value
}
