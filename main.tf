#######################################################
#  
# Overall settings
#
########################################################

locals {
    fleet_project = "abc"
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

########################################################
#  
# Service Account setup in all projects
#
########################################################

# Create Service Account for reporting in the monitoring project

resource "google_service_account" "fleet_service_account" {
  account_id   = "fleetcapture"
  display_name = "Composer Fleet Reporting Capture Service Account"
  provider = google
}

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
  role    = "roles/run.invoker"
  member  = join(":", ["serviceAccount", google_service_account.function_service_account.email])
}

resource "google_cloudfunctions_function" "function" {
  depends_on = [google_project_iam_member.fleet_projects_iam_function]
  name        = "fleetfunc"
  description = "Function refreshing Cloud Composer fleet reports"
  runtime     = "python310"
  region      = local.fleet_report_location

  available_memory_mb   = 512
  source_archive_bucket = replace(google_storage_bucket.fleet_bucket.url, "gs://", "")
  source_archive_object = "function/source.zip"
  trigger_http          = true
  entry_point           = "fleetmon"
  service_account_email = google_service_account.function_service_account.email

}