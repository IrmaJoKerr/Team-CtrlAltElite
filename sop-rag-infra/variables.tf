variable "project_id" {
  description = "The Google Cloud Project ID."
  type        = string
  default     = "ctrlaltelite-484111" # <--- YOUR PROJECT ID HERE
}

variable "region" {
  description = "The primary Google Cloud region for services."
  type        = string
  default     = "us-central1"
}

variable "bucket_name" {
  description = "Name for the Cloud Storage bucket."
  type        = string
  default     = "sop-originals-bucket-ctrlaltelite" # Must be globally unique!
}

variable "bucket_location" {
  description = "Location for the Cloud Storage bucket (region or multi-region). Use a region name for regional buckets, e.g. asia-southeast1."
  type        = string
  default     = "asia-southeast1"
}

variable "pubsub_topic_name" {
  description = "Name for the Pub/Sub topic."
  type        = string
  default     = "sop-processing-topic"
}

variable "cloudsql_instance_name" {
  description = "Name for the Cloud SQL PostgreSQL instance."
  type        = string
  default     = "sop-metadata-db"
}

variable "cloudsql_database_name" {
  description = "Name for the PostgreSQL database within the instance."
  type        = string
  default     = "sop_database"
}

variable "cloudsql_user_name" {
  description = "Name for the PostgreSQL database user."
  type        = string
  default     = "sop_user"
}

variable "secret_name" {
  description = "Name for the Secret Manager secret to store DB password."
  type        = string
  default     = "sop-db-password"
}

variable "processing_service_name" {
  description = "Name for the Cloud Run processing service."
  type        = string
  default     = "processing-service"
}

variable "artifact_registry_repo_name" {
  description = "Name for the Artifact Registry repository."
  type        = string
  default     = "sop-containers"
}
