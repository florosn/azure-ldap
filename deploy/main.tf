terraform {
  required_version = ">= 1.6, < 2.0"
  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}

variable "subscription_id" {
  type = string
}

variable "location" {
  type        = string
  description = "Azure region supporting three AKS zones and RA-GZRS (for example, westeurope)."
  default     = "westeurope"
}

variable "name" {
  type    = string
  default = "azure-ldap"
}

variable "storage_account_name" {
  type        = string
  description = "Globally unique name, 3-24 lowercase letters and digits."
  validation {
    condition     = can(regex("^[a-z0-9]{3,24}$", var.storage_account_name))
    error_message = "Use 3-24 lowercase letters and digits."
  }
}

variable "admin_group_object_ids" {
  type        = list(string)
  description = "Entra group object IDs authorized to administer AKS."
  validation {
    condition     = length(var.admin_group_object_ids) > 0
    error_message = "Specify at least one administrator group."
  }
}

variable "api_server_authorized_ip_ranges" {
  type        = list(string)
  description = "Trusted administrator/CI public CIDRs permitted to reach the Kubernetes API."
  validation {
    condition     = length(var.api_server_authorized_ip_ranges) > 0 && !contains(var.api_server_authorized_ip_ranges, "0.0.0.0/0")
    error_message = "Supply restricted administrator/CI CIDRs."
  }
}

resource "azurerm_resource_group" "ldap" {
  name     = var.name
  location = var.location
}

resource "azurerm_kubernetes_cluster" "ldap" {
  name                              = var.name
  location                          = azurerm_resource_group.ldap.location
  resource_group_name               = azurerm_resource_group.ldap.name
  dns_prefix                        = var.name
  oidc_issuer_enabled               = true
  workload_identity_enabled         = true
  local_account_disabled            = true
  role_based_access_control_enabled = true
  sku_tier                          = "Standard"

  default_node_pool {
    name       = "system"
    node_count = 3
    vm_size    = "Standard_D2s_v5"
    zones      = ["1", "2", "3"]
  }
  identity {
    type = "SystemAssigned"
  }
  azure_active_directory_role_based_access_control {
    tenant_id              = data.azurerm_client_config.current.tenant_id
    admin_group_object_ids = var.admin_group_object_ids
    azure_rbac_enabled     = true
  }
  api_server_access_profile {
    authorized_ip_ranges = var.api_server_authorized_ip_ranges
  }
  network_profile {
    network_plugin      = "azure"
    network_plugin_mode = "overlay"
    network_policy      = "azure"
    load_balancer_sku   = "standard"
  }
}

data "azurerm_client_config" "current" {}

resource "azurerm_storage_account" "backup" {
  name                            = var.storage_account_name
  resource_group_name             = azurerm_resource_group.ldap.name
  location                        = azurerm_resource_group.ldap.location
  account_tier                    = "Standard"
  account_replication_type        = "RAGZRS"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  allow_nested_items_to_be_public = false
  blob_properties {
    versioning_enabled = true
    delete_retention_policy {
      days = 30
    }
    container_delete_retention_policy {
      days = 30
    }
  }
}

resource "azurerm_storage_container" "backup" {
  name                  = "ldap-backups"
  storage_account_id    = azurerm_storage_account.backup.id
  container_access_type = "private"
}

resource "azurerm_user_assigned_identity" "backup" {
  name                = "${var.name}-backup"
  resource_group_name = azurerm_resource_group.ldap.name
  location            = azurerm_resource_group.ldap.location
}

resource "azurerm_federated_identity_credential" "backup" {
  name                = "ldap-backup"
  resource_group_name = azurerm_resource_group.ldap.name
  parent_id           = azurerm_user_assigned_identity.backup.id
  audience            = ["api://AzureADTokenExchange"]
  issuer              = azurerm_kubernetes_cluster.ldap.oidc_issuer_url
  subject             = "system:serviceaccount:ldap:ldap"
}

resource "azurerm_role_assignment" "backup" {
  scope                = azurerm_storage_container.backup.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.backup.principal_id
}

output "resource_group" {
  value = azurerm_resource_group.ldap.name
}
output "cluster_name" {
  value = azurerm_kubernetes_cluster.ldap.name
}
output "backup_client_id" {
  value = azurerm_user_assigned_identity.backup.client_id
}
output "backup_storage_account" {
  value = azurerm_storage_account.backup.name
}
output "secondary_blob_endpoint" {
  value = azurerm_storage_account.backup.secondary_blob_endpoint
}
output "secondary_location" {
  value = azurerm_storage_account.backup.secondary_location
}
