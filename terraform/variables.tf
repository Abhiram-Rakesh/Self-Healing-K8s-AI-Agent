variable "aws_region" {
  description = "AWS region for all resources"
  type        = string
  default     = "ap-south-1"
}

variable "cluster_name" {
  description = "EKS cluster name"
  type        = string
  default     = "self-healing-cluster"
}

variable "cluster_version" {
  description = "Kubernetes version"
  type        = string
  default     = "1.32"
}

variable "environment" {
  description = "dev | staging | prod"
  type        = string
  default     = "dev"
}

variable "system_node_type" {
  description = "EC2 type for system node group"
  type        = string
  default     = "t3.medium"
}

variable "workload_node_type" {
  description = "EC2 type for workload node group"
  type        = string
  default     = "t3.large"
}

variable "system_node_count" {
  description = "Node count for system group"
  type        = number
  default     = 2
}

variable "workload_node_count" {
  description = "Node count for workload group"
  type        = number
  default     = 2
}

variable "enable_ha_nat" {
  description = "true = 3 NAT GWs (HA), false = 1 (cost saving)"
  type        = bool
  default     = false
}

variable "state_bucket" {
  description = "S3 bucket name for Terraform state (must exist before init)"
  type        = string
}

variable "gemini_api_key" {
  description = "Google Gemini API key"
  type        = string
  sensitive   = true
}

variable "slack_webhook_url" {
  description = "Slack webhook URL for notifications (optional)"
  type        = string
  default     = ""
  sensitive   = true
}

variable "tags" {
  description = "Default tags applied to all resources"
  type        = map(string)
  default = {
    Project   = "self-healing-k8s-kagent"
    ManagedBy = "terraform"
  }
}
