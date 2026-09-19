locals {
  # Assembled by hand: `azurerm_storage_container` exports no Resource Manager
  # id, and its `id` is the data-plane URL, which a role assignment rejects.
  state_container_scope = "${azurerm_storage_account.state.id}/blobServices/default/containers/${azurerm_storage_container.state.name}"

  image = "${var.image_registry}/repoagent:${var.image_tag}"

  # The smallest Container Apps allows. This job lists repositories and sends an
  # email; it is bounded by GitHub's response times, not by CPU.
  container_cpu    = 0.25
  container_memory = "0.5Gi"

  # Everything the container needs that is not a secret. Secrets are resolved at
  # runtime from Key Vault by settings.py, not injected here — an env var is
  # visible in `az containerapp job show` output and in state.
  common_env = {
    AZURE_CLIENT_ID                       = azurerm_user_assigned_identity.this.client_id
    KEY_VAULT_URI                         = azurerm_key_vault.this.vault_uri
    APPLICATIONINSIGHTS_CONNECTION_STRING = data.azurerm_application_insights.platform.connection_string
    ENVIRONMENT                           = var.environment
    # Recorded so the digest can say which build produced it. With the job here
    # and the image built in this same repository they move together, but the
    # digest is read a week later and "which code ran" should not need
    # archaeology.
    IMAGE_TAG = var.image_tag
    # Where the run reads and writes its history. Not a secret — it is a URL,
    # and reaching it still needs the managed identity's RBAC grant.
    STATE_CONTAINER_URL = "${azurerm_storage_account.state.primary_blob_endpoint}${azurerm_storage_container.state.name}"
  }
}
