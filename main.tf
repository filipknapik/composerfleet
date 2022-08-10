#######################################################
#  
# Overall settings
#
########################################################

locals {
    fleet_project = "ssss"
    fleet_report_location = "us-central1"
    fleet_bucket_name = "fleetreport"
    composer_projects = toset(["project1", "project2"])
}

#######################################################
#  
# Provider
#
########################################################

provider "google" {
  project = local.fleet_project
  region = local.fleet_report_location
}

#######################################################
#  
# Enable APIs in the fleet monitoring project
#
########################################################

# Enable Composer API

resource "google_project_service" "composer_api" {
  project = local.fleet_project
  service = "composer.googleapis.com"
  provider = google
}

# Enable Storage API

resource "google_project_service" "storage_api" {
  project = local.fleet_project
  service = "storage.googleapis.com"
  provider = google
}

# Enable Cloud Functions API

resource  "google_project_service" "functions_api" {
  project = local.fleet_project
  service = "cloudfunctions.googleapis.com"
  provider = google
}

# Enable Scheduler API

resource "google_project_service" "scheduler_api" {
  project = local.fleet_project
  service = "cloudscheduler.googleapis.com"
  provider = google
}

########################################################
#  
# Storage Buckets setup
#
########################################################

resource "google_storage_bucket" "fleet_bucket" {
  location = local.fleet_report_location
  name     = local.fleet_bucket_name
  uniform_bucket_level_access = true
}

locals {
  bucket = replace(google_storage_bucket.fleet_bucket.url, "gs://", "")
}

########################################################
#  
# Service Account setup in the fleet monitoring project
#
########################################################

# Create Service Account for reporting in the monitoring project

resource "google_service_account" "fleet_service_account" {
  account_id   = "fleetcapture"
  display_name = "Composer Fleet Reporting Capture Service Account"
  provider = google
}

# Grant the service account access to Composer API (enabling the function to retrieve list of supported Composer vesions)

resource "google_project_iam_member" "fleet_sa_iam_composer" {
  depends_on = [google_service_account.fleet_service_account]
  project = local.fleet_project
  role    = "roles/composer.user"
  member  = join(":", ["serviceAccount", google_service_account.fleet_service_account.email])
}

# Grant the service account access to Compute API (enabling the function to retrieve list of Google Cloud regions)

resource "google_project_iam_member" "fleet_sa_iam_compute" {
  depends_on = [google_service_account.fleet_service_account]
  project = local.fleet_project
  role    = "roles/compute.viewer"
  member  = join(":", ["serviceAccount", google_service_account.fleet_service_account.email])
}

# Grant the service account access to Storgae API (enabling the function to save the report.html to the bucket)

resource "google_project_iam_member" "fleet_sa_iam_storage" {
  depends_on = [google_service_account.fleet_service_account]
  project = local.fleet_project
  role    = "roles/storage.objectAdmin"
  member  = join(":", ["serviceAccount", google_service_account.fleet_service_account.email])
}

########################################################
#  
# Service Account setup in all projects
#
########################################################

# In all monitored projects: add Composer User permission to the Service Account of the reporting engine

resource "google_project_iam_member" "fleet_projects_iam_composer" {
  depends_on = [google_service_account.fleet_service_account]
  for_each = local.composer_projects
  project = "${each.value}"
  role    = "roles/composer.user"
  member  = join(":", ["serviceAccount", google_service_account.fleet_service_account.email])
}

# In all monitored projects: add Monitoring Viewer permission to the Service Account of the reporting engine

resource "google_project_iam_member" "fleet_projects_iam_monitoring" {
  depends_on = [google_service_account.fleet_service_account]
  for_each = local.composer_projects
  project = "${each.value}"
  role    = "roles/monitoring.viewer"
  member  = join(":", ["serviceAccount", google_service_account.fleet_service_account.email])
}

data "http" "function_source_main_py" {
  url = "https://github.com/filipknapik/composerfleet/blob/main/Function/main.py?raw=true"

}

data "http" "function_source_requirements_txt" {
  url = "https://github.com/filipknapik/composerfleet/blob/main/Function/requirements.txt?raw=true"

}

data "archive_file" "zip_function_source_code" {
  depends_on = [data.http.function_source_main_py, data.http.function_source_requirements_txt]
  type        = "zip"
  output_path = "${path.module}/Archive.zip"

  source {
    content  = "${data.http.function_source_main_py.response_body}"
    filename = "main.py"
  }

  source {
    content  = "${data.http.function_source_requirements_txt.response_body}"
    filename = "requirements.txt"
  }
}

resource "google_storage_bucket_object" "function_code" {
  depends_on = [data.archive_file.zip_function_source_code]
  name   = "function/source.zip"
  source = "${path.module}/Archive.zip"
  bucket = replace(google_storage_bucket.fleet_bucket.url, "gs://", "")
}


resource "google_service_account" "function_service_account" {
  account_id   = "funcinvoker"
  display_name = "Composer Fleet Reporting Function Invoker Service Account"
  provider = google
}

# In all monitored projects: add Composer User permission to the Service Account of the reporting engine

resource "google_project_iam_member" "fleet_projects_iam_function" {
  depends_on = [google_service_account.function_service_account]
  project = local.fleet_project
  role    = "roles/cloudfunctions.invoker"
  member  = join(":", ["serviceAccount", google_service_account.function_service_account.email])
}

resource "google_cloudfunctions_function" "refresh_function" {
  depends_on = [google_project_iam_member.fleet_projects_iam_function]
  name        = "fleetfunc"
  description = "Function refreshing Cloud Composer fleet reports"
  runtime     = "python310"
  region      = local.fleet_report_location

  available_memory_mb   = 512
  source_archive_bucket = local.bucket
  source_archive_object = "function/source.zip"
  trigger_http          = true
  entry_point           = "fleetmon"
  service_account_email = google_service_account.fleet_service_account.email

  environment_variables = {
    PROJECT_ID = local.fleet_project
    PROJECTS = join(",",local.composer_projects)
    BUCKET = local.bucket
  }
}

resource "google_cloud_scheduler_job" "refresh_job" {
  name             = "fleet_refresh_job"
  description      = "Cloud Composer fleet report "
  schedule         = "0 * * * *"
  time_zone        = "America/New_York"
  attempt_deadline = "600s"

  http_target {
    http_method = "POST"
    uri         = google_cloudfunctions_function.refresh_function.https_trigger_url

    oidc_token {
      service_account_email = google_service_account.function_service_account.email
      audience = google_cloudfunctions_function.refresh_function.https_trigger_url
    }
  }
}