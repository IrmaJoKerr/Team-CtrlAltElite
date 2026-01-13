variable "project_id" {
  type        = string
  description = "GCP project ID"
}

variable "region" {
  type        = string
  description = "GCP region"
}

variable "display_name" {
  type        = string
  description = "Vertex AI RAG corpus display name"
}

variable "description" {
  type        = string
  description = "Vertex AI RAG corpus description"
}
