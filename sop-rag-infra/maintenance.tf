// Bucket maintenance Cloud Run service and daily scheduler
resource "google_cloud_run_v2_service" "bucket_maintenance" {
  name     = "bucket-maintenance"
  location = var.region
  template {
    containers {
      image = "us-central1-docker.pkg.dev/${var.project}/sop-containers/bucket-maintenance:latest"
      env {
        name  = "BUCKET_NAME"
        value = google_storage_bucket.sop_originals_bucket.name
      }
      env {
        name  = "THRESHOLD_HOURS"
        value = "24"
      }
      ports {
        container_port = 8080
      }
    }
    service_account = google_service_account.processing_service_sa.email
  }
}

resource "google_cloud_scheduler_job" "bucket_maintenance_schedule" {
  name        = "bucket-maintenance-daily"
  description = "Call bucket-maintenance every day to flush if idle"
  schedule    = "0 0 * * *" // daily at 00:00 UTC
  time_zone   = "UTC"

  http_target {
    http_method = "POST"
    uri         = google_cloud_run_v2_service.bucket_maintenance.uri
    // use existing pubsub-push-sa for OIDC token (must have run.invoker role)
    oidc_token {
      service_account_email = google_service_account.pubsub_push_sa.email
    }
  }
}
