#######################################################
#  
# Overall settings
#
########################################################

locals {
    fleet_project = "fleetproj"
    fleet_report_location = "us-central1"
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
# Storage Bucket setup
#
########################################################

resource "google_storage_bucket" "fleet_report_bucket" {
  location = local.fleet_report_location
  name     = "fleetreport"
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



