# The weekly scan.
#
# Runs on the shared platform environment (see data.tf) but lives in this
# project's own resource group. That is allowed across resource groups, but not
# across regions, which is why the resource group takes its location from the
# environment rather than from a variable of its own.
resource "azurerm_container_app_job" "scan" {
  name                         = module.naming_scan.container_app_job.name
  container_app_environment_id = data.azurerm_container_app_environment.platform.id
  resource_group_name          = azurerm_resource_group.this.name
  # Jobs require a location explicitly; container apps inherit the
  # environment's. It must match the environment's region.
  location = azurerm_resource_group.this.location

  # The scan makes a few dozen HTTP requests and sends one email. Fifteen
  # minutes is loose rather than considered — it exists so a hung connection
  # ends the run rather than billing until someone notices.
  replica_timeout_in_seconds = 900
  # One retry. The scan is idempotent — it reads and reports, and changes
  # nothing — so a retry is safe, but a second failure is a real failure worth
  # hearing about rather than papering over.
  replica_retry_limit = 1

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.this.id]
  }

  # Sunday evening, so the digest is waiting on Monday morning without being
  # measured during Monday's Renovate burst. The shared Renovate preset opens
  # every repository's pull requests `before 6am on monday` and branch
  # protection drains them one per Renovate run, so a Monday-morning scan
  # counts a healthy queue mid-flight as a backlog. Sunday is the point of
  # maximum drain, and is also off-peak for DeepSeek, which bills weekdays only.
  # Evaluated in UTC with five fields and no seconds field, so the wall-clock
  # time shifts with British Summer Time.
  #
  # ForceNew: changing the cron replaces the job rather than updating it. That
  # is harmless here, since the job holds no state.
  schedule_trigger_config {
    cron_expression          = var.scan_cron_expression
    parallelism              = 1
    replica_completion_count = 1
  }

  template {
    container {
      name   = "scan"
      image  = local.image
      cpu    = local.container_cpu
      memory = local.container_memory

      # **No `command` argument, deliberately.** The image's ENTRYPOINT already
      # names the console script, and repeating it here would be a second source
      # of truth for one string — one that Terraform cannot see change.
      #
      # market-agent took a full outage from exactly that in September 2026: its
      # `command = ["marketagent"]` sat under `ignore_changes`, went stale when
      # the console script was renamed, and every workload crash-looped on
      # `exec: "investagent": executable file not found`. The Terraform there had
      # already been updated; the ignore meant the apply never pushed it, and the
      # az-cli deploy path does not touch `command` at all.
      #
      # `args` picks the subcommand, and is deliberately **not** ignored below: it
      # changes rarely, and a subcommand that does not exist fails loudly with a
      # usage error rather than silently running the wrong workload.
      args = ["scan"]

      dynamic "env" {
        for_each = local.common_env
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  tags = local.tags

  # `make deploy` (az cli) owns the running image and env after the first
  # revision, so that a deploy needs no state lock, no plan of unrelated drift,
  # and no risk of a stale local tfvars rolling the image backwards.
  #
  # The whole `env` map is ignored rather than just IMAGE_TAG's entry, because
  # indexing into a map-driven `dynamic` block by position would silently shift
  # if common_env gained or lost a key. The trade-off: a change to any *other*
  # value in common_env needs a `make deploy` to land on a running revision —
  # `terraform plan` will report no diff even though the value it computes has
  # moved.
  #
  # Note `command` is absent from this list because it is absent from the
  # resource. That is the point.
  lifecycle {
    ignore_changes = [
      template[0].container[0].image,
      template[0].container[0].env,
    ]
  }
}
