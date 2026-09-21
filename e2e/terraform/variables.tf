variable "region" {
  description = "AWS region. Same account/region as netapp_test by decision; state is independent."
  type        = string
  default     = "us-east-1"
}

variable "prefix" {
  description = "Name prefix on every resource, so the whole harness is easy to find and destroy."
  type        = string
  default     = "cohesity-e2e"
}

variable "instance_type" {
  description = "Environment ActiveGate minimum is 2 vCPU / 4 GB; t3.medium clears it (netapp_test's default)."
  type        = string
  default     = "t3.medium"
}

# --- Dynatrace -----------------------------------------------------------------

variable "dt_environment_url" {
  description = "CLASSIC tenant URL the installer is downloaded from, no trailing slash: https://<env>.live.dynatrace.com, or https://<env>.sprint.dynatracelabs.com on sprint. A trailing /api is tolerated."
  type        = string
  validation {
    condition     = can(regex("^https://", var.dt_environment_url)) && !strcontains(var.dt_environment_url, ".apps.")
    error_message = "Use the classic host (https://<env>.live.dynatrace.com or https://<env>.sprint.dynatracelabs.com), not the .apps. host - the deployment API is served from the classic host."
  }
}

variable "dt_paas_token" {
  description = "Installer token (InstallerDownload scope, e.g. a PaaS or Dynatrace Operator token). Supply via TF_VAR_dt_paas_token or a gitignored tfvars - never commit it."
  type        = string
  sensitive   = true
}

variable "activegate_group" {
  description = "ActiveGate group. The e2e monitoring configuration is scoped to ag_group-<this>, which keeps it off every other ActiveGate on a shared tenant."
  type        = string
  default     = "cohesity-e2e"
}

variable "extension_ca_cert_path" {
  description = "PUBLIC extension-signing CA certificate. Copied onto the ActiveGate so it trusts packages signed with the matching developer certificate."
  type        = string
  default     = "C:/Users/Cooper/.dynatrace/certificates/ca.pem"
}

# --- Fake cluster source -------------------------------------------------------

variable "repo_url" {
  description = "Public git URL of this repository; the instance clones it to run tools/local_cohesity_server.py."
  type        = string
  default     = "https://github.com/cooperjfecteau-cell/cohesity-storage-ext.git"
}

variable "repo_ref" {
  description = "Branch, tag or commit to check out. Push drift mode (and e2e/scripts) before applying, or the clone will not have them."
  type        = string
  default     = "master"
}
