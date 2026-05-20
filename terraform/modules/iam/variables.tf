variable "cluster_name" {
  description = "EKS cluster name (used for resource naming)"
  type        = string
}

variable "oidc_provider_arn" {
  description = "ARN of the IAM OIDC provider for IRSA"
  type        = string
}

variable "oidc_provider_url" {
  description = "OIDC issuer URL of the cluster (no https:// prefix)"
  type        = string
}

variable "namespace" {
  description = "Kubernetes namespace where the agent runs"
  type        = string
  default     = "default"
}

variable "service_account_name" {
  description = "Kubernetes ServiceAccount name used by the agent"
  type        = string
  default     = "kagent-healer"
}

variable "gemini_api_key" {
  description = "Gemini API key (stored in AWS Secrets Manager)"
  type        = string
  sensitive   = true
}

variable "slack_webhook_url" {
  description = "Slack webhook URL (stored in AWS Secrets Manager, optional)"
  type        = string
  sensitive   = true
  default     = ""
}

variable "tags" {
  description = "Tags applied to all IAM/Secrets resources"
  type        = map(string)
  default     = {}
}
