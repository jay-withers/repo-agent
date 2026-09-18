# One identity for this project, used by the job to read its own Key Vault.
#
# Per-project rather than one shared across the platform: identities are free,
# and this one holds the GitHub App private key — a credential over every
# repository the App is installed on. A platform-wide identity would make that
# key readable by any future project's image.
resource "azurerm_user_assigned_identity" "this" {
  name                = module.naming.user_assigned_identity.name
  resource_group_name = azurerm_resource_group.this.name
  location            = azurerm_resource_group.this.location
  tags                = local.tags
}
