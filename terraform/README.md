# terraform

<!-- BEGIN_TF_DOCS -->
## Requirements

| Name | Version |
| ---- | ------- |
| <a name="requirement_terraform"></a> [terraform](#requirement\_terraform) | >= 1.6 |
| <a name="requirement_azurerm"></a> [azurerm](#requirement\_azurerm) | ~> 5.0 |
| <a name="requirement_random"></a> [random](#requirement\_random) | >= 3.3.2 |

## Providers

| Name | Version |
| ---- | ------- |
| <a name="provider_azurerm"></a> [azurerm](#provider\_azurerm) | 5.6.0 |

## Modules

| Name | Source | Version |
| ---- | ------ | ------- |
| <a name="module_naming"></a> [naming](#module\_naming) | Azure/naming/azurerm | ~> 0.4 |
| <a name="module_naming_scan"></a> [naming\_scan](#module\_naming\_scan) | Azure/naming/azurerm | ~> 0.4 |

## Resources

| Name | Type |
| ---- | ---- |
| [azurerm_container_app_job.scan](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/container_app_job) | resource |
| [azurerm_key_vault.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/key_vault) | resource |
| [azurerm_resource_group.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/resource_group) | resource |
| [azurerm_role_assignment.deployer_secrets_officer](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/role_assignment) | resource |
| [azurerm_role_assignment.deployer_state_contributor](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/role_assignment) | resource |
| [azurerm_role_assignment.identity_secrets_user](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/role_assignment) | resource |
| [azurerm_role_assignment.identity_state_contributor](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/role_assignment) | resource |
| [azurerm_storage_account.state](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/storage_account) | resource |
| [azurerm_storage_container.state](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/storage_container) | resource |
| [azurerm_user_assigned_identity.this](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/resources/user_assigned_identity) | resource |
| [azurerm_application_insights.platform](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/data-sources/application_insights) | data source |
| [azurerm_client_config.current](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/data-sources/client_config) | data source |
| [azurerm_container_app_environment.platform](https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/data-sources/container_app_environment) | data source |

## Inputs

| Name | Description | Type | Default | Required |
| ---- | ----------- | ---- | ------- | :------: |
| <a name="input_environment"></a> [environment](#input\_environment) | Deployment environment. Drives resource naming, and selects which shared platform environment this project deploys onto. | `string` | n/a | yes |
| <a name="input_image_registry"></a> [image\_registry](#input\_image\_registry) | Registry and repository prefix the image is pulled from. A public package on ghcr.io deliberately: a private one would need a `registry` block and a Key Vault-backed pull secret on the job, and there is no Azure Container Registry because ACR Basic is a flat monthly charge with no consumption tier. | `string` | `"ghcr.io/jay-withers/repo-agent"` | no |
| <a name="input_image_tag"></a> [image\_tag](#input\_image\_tag) | Image tag seeding the job's **first** revision only. After that the container's image and env are under `lifecycle.ignore_changes` and `make deploy` owns them, so a plan against an existing job reports no change here even when the running image has moved on. Don't read this as the deployed version; check `az containerapp job show`. | `string` | `"v0.0.1"` | no |
| <a name="input_key_vault_administrator_object_ids"></a> [key\_vault\_administrator\_object\_ids](#input\_key\_vault\_administrator\_object\_ids) | Additional Entra object IDs granted `Key Vault Secrets Officer`, so they can populate secret values. Whoever runs `terraform apply` gets this automatically. | `list(string)` | `[]` | no |
| <a name="input_platform_app_insights_name"></a> [platform\_app\_insights\_name](#input\_platform\_app\_insights\_name) | Name of the shared Application Insights instance the job reports telemetry to. | `string` | `"appi-platform-dev"` | no |
| <a name="input_platform_environment_name"></a> [platform\_environment\_name](#input\_platform\_environment\_name) | Name of the shared Container Apps environment this job runs on. | `string` | `"cae-platform-dev"` | no |
| <a name="input_platform_resource_group_name"></a> [platform\_resource\_group\_name](#input\_platform\_resource\_group\_name) | Resource group holding the shared Container Apps environment. | `string` | `"rg-platform-dev"` | no |
| <a name="input_project_name"></a> [project\_name](#input\_project\_name) | Project name included in every resource name. Lowercase only (container app jobs reject uppercase), and short enough that `caj-<project>-<env>-scan` fits the 32 characters container app jobs allow — the naming module truncates silently rather than failing. Changing this is also how to resolve a clash on the globally unique Key Vault name. | `string` | `"repoagent"` | no |
| <a name="input_scan_cron_expression"></a> [scan\_cron\_expression](#input\_scan\_cron\_expression) | When the scan runs, in UTC. Five fields, no seconds field, so the wall-clock time shifts with British Summer Time. Monday morning by default, so the week's findings are waiting rather than arriving mid-week. | `string` | `"0 7 * * 1"` | no |
| <a name="input_tags"></a> [tags](#input\_tags) | Tags applied to all resources, merged with (and taking precedence over) the default tags (`environment`, `managed-by`). | `map(string)` | `{}` | no |

## Outputs

| Name | Description |
| ---- | ----------- |
| <a name="output_identity_client_id"></a> [identity\_client\_id](#output\_identity\_client\_id) | Client ID of the workload identity, which the container receives as `AZURE_CLIENT_ID` and uses to read Key Vault. |
| <a name="output_key_vault_name"></a> [key\_vault\_name](#output\_key\_vault\_name) | Key Vault name, for populating secret values with `az keyvault secret set`. |
| <a name="output_resource_group_name"></a> [resource\_group\_name](#output\_resource\_group\_name) | This project's resource group. |
| <a name="output_scan_job_name"></a> [scan\_job\_name](#output\_scan\_job\_name) | Name of the scan job, for `az containerapp job update` and `az containerapp job start`. |
| <a name="output_state_container_url"></a> [state\_container\_url](#output\_state\_container\_url) | Blob container holding the scan's history, for `make deploy` and for `repoagent suppress` run locally. |
<!-- END_TF_DOCS -->
