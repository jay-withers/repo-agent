output "resource_group_name" {
  description = "This project's resource group."
  value       = azurerm_resource_group.this.name
}

output "key_vault_name" {
  description = "Key Vault name, for populating secret values with `az keyvault secret set`."
  value       = azurerm_key_vault.this.name
}

# What `make deploy` resolves before calling `az containerapp job update`, and
# what `az containerapp job start` needs to run the scan by hand — a scheduled
# job has no other way to be triggered.
output "scan_job_name" {
  description = "Name of the scan job, for `az containerapp job update` and `az containerapp job start`."
  value       = azurerm_container_app_job.scan.name
}

output "identity_client_id" {
  description = "Client ID of the workload identity, which the container receives as `AZURE_CLIENT_ID` and uses to read Key Vault."
  value       = azurerm_user_assigned_identity.this.client_id
}

# What `make deploy` pushes onto the running revision, because `common_env` sits
# under `ignore_changes` and Terraform will therefore never update it itself.
output "state_container_url" {
  description = "Blob container holding the scan's history, for `make deploy` and for `repoagent suppress` run locally."
  value       = "${azurerm_storage_account.state.primary_blob_endpoint}${azurerm_storage_container.state.name}"
}
