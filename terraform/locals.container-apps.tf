locals {
  # Assembled by hand: `azurerm_storage_container` exports no Resource Manager
  # id, and its `id` is the data-plane URL, which a role assignment rejects.
  state_container_scope = "${azurerm_storage_account.state.id}/blobServices/default/containers/${azurerm_storage_container.state.name}"

  # **The first revision runs a placeholder, not the scanner.** Terraform only
  # needs an image to create the job; the first `make deploy` (cd-publish, on
  # the first release) puts the real one on, and the image is under
  # `ignore_changes`, so Terraform never touches it again. Inside a job the
  # placeholder exits 0 at once. This replaced seeding from a guessed release
  # tag, which passes plan and apply and only fails when the job pulls it.
  #
  # `latest` deliberately: dev-containers prunes all but its last ten releases
  # and never `latest`, and a moving tag's usual trap (a re-push deploys
  # nothing) can't apply to an image used only at create.
  #
  # **A job that is replaced (a ForceNew change) comes back on this** and then
  # "succeeds" doing nothing. Run `make deploy` after any apply that replaces it.
  image = "ghcr.io/jay-withers/dev-containers/placeholder:latest"

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
    # IMAGE_TAG, which the digest prints so "which code ran" needs no
    # archaeology, is set by `make deploy` alongside the image: the placeholder
    # Terraform creates with has no release tag.
    # Where the run reads and writes its history. Not a secret — it is a URL,
    # and reaching it still needs the managed identity's RBAC grant.
    STATE_CONTAINER_URL = "${azurerm_storage_account.state.primary_blob_endpoint}${azurerm_storage_container.state.name}"
  }
}
