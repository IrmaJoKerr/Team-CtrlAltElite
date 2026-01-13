# The 'terraform { required_providers { ... } }' block should NOT be here if it's in versions.tf

provider "google" {
  project = var.project_id
  region  = var.region
}

# --- Artifact Registry for Docker Images ---
resource "google_artifact_registry_repository" "sop_containers_repo" {
  location      = var.region
  repository_id = var.artifact_registry_repo_name
  description   = "Docker images for SOP application"
  format        = "DOCKER"
  project       = var.project_id
}

# --- Cloud Storage Bucket for SOP Documents ---
resource "google_storage_bucket" "sop_originals_bucket" {
  name                        = var.bucket_name
  location                    = var.bucket_location # Parameterized location (region or multi-region)
  project                     = var.project_id
  uniform_bucket_level_access = true  # Enforce uniform access control
  force_destroy               = false # Prevent accidental deletion of non-empty bucket

  lifecycle_rule {
    action {
      type = "Delete"
    }
    condition {
      age = 365 # Delete objects older than 365 days, adjust as needed
    }
  }
}

# --- Pub/Sub Topic for Ingestion Notifications ---
resource "google_pubsub_topic" "sop_processing_topic" {
  name    = var.pubsub_topic_name
  project = var.project_id
}

# --- Data source to reference the existing 'default' VPC network ---
data "google_compute_network" "default" {
  project = var.project_id
  name    = "default"
}

# Connect GCS to Pub/Sub: Send notifications when new objects are created
resource "google_storage_notification" "gcs_to_pubsub_notification" {
  bucket         = google_storage_bucket.sop_originals_bucket.name
  payload_format = "JSON_API_V1"
  topic          = google_pubsub_topic.sop_processing_topic.id
  event_types    = ["OBJECT_FINALIZE"] # Trigger on new object creation/overwrite
}

# --- Cloud SQL PostgreSQL Instance ---
resource "google_sql_database_instance" "sop_metadata_db" {
  database_version = "POSTGRES_15"
  name             = var.cloudsql_instance_name
  project          = var.project_id
  region           = var.region
  settings {
    tier      = "db-f1-micro" # Smallest for hackathon MVP, adjust for production
    disk_size = 20            # Minimum disk size
    disk_type = "PD-SSD"
    ip_configuration {
      ipv4_enabled    = false
      private_network = "projects/${var.project_id}/global/networks/default" # Use default VPC network
    }
    backup_configuration {
      enabled            = true
      start_time         = "03:00"
    }
    maintenance_window {
      day  = 7 # Sunday
      hour = 0 # Midnight
    }
  }
  depends_on = [
    google_service_networking_connection.private_vpc_connection
  ]
}

resource "google_compute_global_address" "private_ip_alloc" {
  project       = var.project_id
  name          = "google-managed-services-private-ip"
  purpose       = "VPC_PEERING"
  address_type  = "INTERNAL"
  prefix_length = 20
  # BEFORE: network       = "projects/${var.project_id}/global/networks/default"
  network       = data.google_compute_network.default.id # <--- CHANGED THIS LINE
}

resource "google_service_networking_connection" "private_vpc_connection" {
  # BEFORE: network                 = google_compute_network.default.id
  network                 = data.google_compute_network.default.id # <--- CHANGED THIS LINE
  service                 = "servicenetworking.googleapis.com"
  reserved_peering_ranges = [google_compute_global_address.private_ip_alloc.name]
}

# Database within the instance
resource "google_sql_database" "sop_database" {
  name      = var.cloudsql_database_name
  instance  = google_sql_database_instance.sop_metadata_db.name
  project   = var.project_id
  charset   = "UTF8"
  collation = "en_US.UTF8"
}

# --- Random Password Generation for Cloud SQL User ---
resource "random_password" "sql_db_password" {
  length           = 16
  special          = true
  override_special = "!@#$%&*" # Specify allowed special characters
  min_special      = 1
  min_numeric      = 1
  min_upper        = 1
  min_lower        = 1
}

# Database User
resource "google_sql_user" "sop_user" {
  name     = var.cloudsql_user_name
  instance = google_sql_database_instance.sop_metadata_db.name
  # BEFORE: password = var.cloudsql_password == "" ? null : var.cloudsql_password # Use generated if empty
  password = random_password.sql_db_password.result # <-- CHANGED THIS LINE
  project  = var.project_id
}


# --- Secret Manager for DB Password ---
resource "google_secret_manager_secret" "db_password_secret" {
  secret_id = var.secret_name
  project   = var.project_id
  replication {
    auto {}
  }
}

# Add the generated (or provided) password as the initial secret version
resource "google_secret_manager_secret_version" "db_password_secret_version" {
  secret      = google_secret_manager_secret.db_password_secret.id
  # BEFORE: secret_data = sensitive(google_sql_user.sop_user.password)
  secret_data = sensitive(random_password.sql_db_password.result) # <-- CHANGED THIS LINE
}

# --- Cloud Run Service Definition (Processing Service) ---
resource "google_cloud_run_v2_service" "processing_service" {
  name     = var.processing_service_name
  location = var.region
  project  = var.project_id

  template {
    service_account = google_service_account.processing_service_sa.email

    containers {
      image = "${var.region}-docker.pkg.dev/${var.project_id}/${var.artifact_registry_repo_name}/${var.processing_service_name}:latest"

      ports {
        container_port = 8080
      }

      env {
        name  = "PROJECT_ID"
        value = var.project_id
      }

      env {
        name  = "DB_INSTANCE_CONNECTION_NAME"
        value = google_sql_database_instance.sop_metadata_db.connection_name
      }

      env {
        name  = "DB_NAME"
        value = google_sql_database.sop_database.name
      }

      env {
        name  = "DB_USER"
        value = google_sql_user.sop_user.name
      }

      env {
        name  = "DB_SECRET_NAME"
        value = google_secret_manager_secret.db_password_secret.secret_id
      }

      env {
        name  = "GCS_BUCKET_NAME"
        value = google_storage_bucket.sop_originals_bucket.name
      }

      env {
        name  = "RAG_CORPUS_NAME"
        value = module.sop_rag_corpus.rag_corpus_name
      }
    }
  }

  traffic {
    type    = "TRAFFIC_TARGET_ALLOCATION_TYPE_LATEST"
    percent = 100
  }

  ingress = "INGRESS_TRAFFIC_ALL"
}

# Create a Pub/Sub subscription for the processing service (Push)
resource "google_pubsub_subscription" "processing_service_sub" {
  name                 = "${var.processing_service_name}-sub"
  topic                = google_pubsub_topic.sop_processing_topic.name
  ack_deadline_seconds = 600 # 10 minutes, adjust based on processing time
  project              = var.project_id

  push_config {
    push_endpoint = google_cloud_run_v2_service.processing_service.uri
    oidc_token {
      service_account_email = google_service_account.pubsub_push_sa.email
    }
  }
  # Grant Pub/Sub SA permission to invoke Cloud Run
  depends_on = [google_project_iam_member.pubsub_to_cloudrun_invoker]
}

# --- Service Accounts for Least Privilege ---
resource "google_service_account" "processing_service_sa" {
  account_id   = "${var.processing_service_name}-sa"
  display_name = "Service Account for Processing Cloud Run Service"
  project      = var.project_id
}

resource "google_service_account" "pubsub_push_sa" {
  account_id   = "pubsub-push-sa"
  display_name = "Service Account for Pub/Sub Push Subscriptions"
  project      = var.project_id
}

# --- IAM Permissions ---
# Cloud Run SA needs to pull images from Artifact Registry
resource "google_project_iam_member" "cloudrun_artifact_reader" {
  project = var.project_id
  role    = "roles/artifactregistry.reader"
  member  = "serviceAccount:${google_service_account.processing_service_sa.email}"
}

# Cloud Run SA needs to access Secret Manager
resource "google_secret_manager_secret_iam_member" "processing_service_secret_accessor" {
  secret_id = google_secret_manager_secret.db_password_secret.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.processing_service_sa.email}"
}

# Cloud Run SA needs to read from GCS bucket
resource "google_storage_bucket_iam_member" "processing_service_gcs_reader" {
  bucket = google_storage_bucket.sop_originals_bucket.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.processing_service_sa.email}"
}

# Cloud Run SA needs to manage Cloud SQL connections
resource "google_project_iam_member" "processing_service_cloudsql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.processing_service_sa.email}"
}

# Cloud Run SA needs to write to Cloud SQL database
# (Roles for specific DB operations will be handled by application code and DB user permissions)
resource "google_project_iam_member" "processing_service_cloudsql_editor" {
  project = var.project_id
  role    = "roles/cloudsql.editor" # For hackathon, simplify. In prod, more granular DB user grants.
  member  = "serviceAccount:${google_service_account.processing_service_sa.email}"
}

# Cloud Run SA needs to invoke Vertex AI APIs
resource "google_project_iam_member" "processing_service_vertex_ai_user" {
  project = var.project_id
  role    = "roles/aiplatform.user" # Grants permissions to use Vertex AI services
  member  = "serviceAccount:${google_service_account.processing_service_sa.email}"
}

# Pub/Sub SA needs to invoke the Cloud Run processing service
resource "google_project_iam_member" "pubsub_to_cloudrun_invoker" {
  project = var.project_id
  role    = "roles/run.invoker"
  member  = "serviceAccount:${google_service_account.pubsub_push_sa.email}"
}

# --- Vertex AI RAG Engine (using a module workaround) ---
module "sop_rag_corpus" {
  source = "./modules/rag_corpus_creator"

  project_id   = var.project_id
  region       = var.region
  display_name = "sop-rag-corpus"
  description  = "Corpus for SOP documents, managed by Terraform via module"
}

# --- Enable AI Platform API (if not already via gcloud) ---
resource "google_project_service" "aiplatform_api" {
  project            = var.project_id
  service            = "aiplatform.googleapis.com"
  disable_on_destroy = false
}
