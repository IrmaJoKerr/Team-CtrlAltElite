output "gcs_bucket_name" {
  description = "The name of the Cloud Storage bucket for SOP documents."
  value       = google_storage_bucket.sop_originals_bucket.name
}

output "pubsub_topic_name" {
  description = "The name of the Pub/Sub topic for ingestion."
  value       = google_pubsub_topic.sop_processing_topic.name
}

output "cloud_run_service_url" {
  description = "The URL of the Cloud Run processing service."
  value       = google_cloud_run_v2_service.processing_service.uri
}

output "cloud_sql_instance_connection_name" {
  description = "The connection name for the Cloud SQL instance (e.g., for Cloud SQL Proxy)."
  value       = google_sql_database_instance.sop_metadata_db.connection_name
}

output "cloud_sql_database_name" {
  description = "The name of the database within Cloud SQL."
  value       = google_sql_database.sop_database.name
}

output "cloud_sql_user_name" {
  description = "The username for the Cloud SQL database."
  value       = google_sql_user.sop_user.name
}

output "secret_manager_id" {
  description = "The ID of the Secret Manager secret holding the DB password."
  value       = google_secret_manager_secret.db_password_secret.id
}

output "artifact_registry_repository" {
  description = "The full path to the Artifact Registry Docker repository."
  value       = google_artifact_registry_repository.sop_containers_repo.name
}

output "rag_corpus_name_full" {
  description = "The full resource name of the Vertex AI RAG Corpus."
  value       = module.sop_rag_corpus.rag_corpus_name
}

output "processing_service_sa_email" {
  description = "Email of the service account used by the processing service."
  value       = google_service_account.processing_service_sa.email
}
