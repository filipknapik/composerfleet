import functions_framework
from google.cloud.orchestration.airflow import service_v1
from google.cloud import monitoring_v3
from google.cloud import compute_v1
from google.cloud import storage
from threading import Thread
from queue import Queue
import time
import os
import datetime
import re
import math

def get_all_regions(project):
    # Get a list of all regions from Compute Engine's API in the current project

    regions = []                                        # array where we will store all Google Cloud regions
    initialize_params = compute_v1.RegionsClient()
    try:
        regions_list = initialize_params.list(project=project)          # retrieve a list of regions in the current project
        for x in regions_list:
            regions.append(x.name)              
    except Exception as e:
        raise RuntimeError("No access to Compute Engine API in the current project. Please check if Compute Engine API is enabled in the current project and if the function's Service Account has Compute Viewer role.")
    return regions

def get_all_versions(project, months_of_support):
    # Get all versions of Cloud Composer including their release dates. Then, calculate remaining months of support for each version 

    img_version_client = service_v1.ImageVersionsClient()
    today = datetime.date.today()                   # Get today's date

    try:
        request = service_v1.ListImageVersionsRequest(parent="projects/"+project+"/locations/us-central1", page_size = 10, include_past_releases = True)
        versions_result = img_version_client.list_image_versions(request=request)
    except Exception as e:
        raise RuntimeError("No access to Composer API in the current project. Please check if Composer API is enabled in the current project and if the function's Service Account has Compuser User role.")
    
    versions_support = {}                           # Dictionary where we will store details of each version

    for response in versions_result:
        searched_terms = re.search('composer-(.+?)-airflow-(.*)', response.image_version_id) 
        if searched_terms and hasattr(response,'release_date'):
            if hasattr(response.release_date, 'year'):
                if response.release_date.year > 2016:
                    version = searched_terms.group(1)               # Get Composer version (Airflow version is irrelevant)
                    months_left = str(math.floor(max(0,months_of_support - (today - datetime.date(response.release_date.year, response.release_date.month, response.release_date.day)).days/30)))
                    versions_support[version] = months_left         # Save remaining months of support for each version
    return versions_support

def list_envs(project, region, versions_support, queue):
    envs = []
    list_envs_result = {}
    new_env = {}
    to_remove = None
    page_result = None
    today = datetime.date.today()
    env_client = service_v1.EnvironmentsClient()

    try:
        request = service_v1.ListEnvironmentsRequest(parent="projects/"+project+"/locations/"+region)
        page_result = env_client.list_environments(request=request)
    except BaseException as error:
        to_remove = region
        if str(error).find("403")>=0:
            list_envs_result['error'] = "Function's Service Account got permission denied (403) when trying to access Composer API in project "+project + ". Ensure that this Service Account has Composer User role for this project."
            list_envs_result['project'] = project
            queue.put(list_envs_result)
            return

    # Handle the response
    if page_result:
        for response in page_result:
            new_env = {}
            
            searched_terms = re.search('projects/(.+?)/locations/(.+?)/environments/(.*)', response.name)
            if searched_terms:
                new_env['project'] = searched_terms.group(1)
                new_env['location'] = searched_terms.group(2)
                new_env['environment'] = searched_terms.group(3)
                new_env['url'] = "https://console.cloud.google.com/composer/environments/detail/" + new_env['location'] + "/" + new_env['environment'] + "?project=" + new_env['project']

            new_env['state'] = str(response.state).replace('State.','')

            searched_terms = re.search('composer-(.+?)-airflow-(.*)', response.config.software_config.image_version)
            if searched_terms:
                new_env['composer_version'] = searched_terms.group(1)
                new_env['airflow_version'] = searched_terms.group(2)

            if hasattr(response.config.private_environment_config, 'enable_private_environment'):
                if response.config.private_environment_config.enable_private_environment:
                    new_env['private'] = 'Y'
                else:
                    new_env['private'] = 'N'
            else:
                new_env.private = 'N'

            new_env['created'] = str((today - datetime.date.fromtimestamp(response.create_time.timestamp())).days)
            new_env['updated'] = str((today - datetime.date.fromtimestamp(response.update_time.timestamp())).days)
            new_env['support'] = versions_support[new_env['composer_version']]
            env_added = True
            envs.append(new_env)

    list_envs_result['envs'] = envs
    list_envs_result['to_remove'] = to_remove
    queue.put(list_envs_result)
    return

def get_metric(project, metric, aligner):
    client = monitoring_v3.MetricServiceClient()
    project_name = f"projects/" + project
    print("retrieving for " + project)

    now = time.time()
    seconds = int(now)
    nanos = int((now - seconds) * 10 ** 9)

    interval = monitoring_v3.TimeInterval(
        {
            "end_time": {"seconds": seconds, "nanos": 0},
            "start_time": {"seconds": (seconds - 24*3600), "nanos": 0},
        }
    )
    results = client.list_time_series(
        request={
            "name": project_name,
            "filter": 'metric.type = "' + metric + '"',
            "interval": interval,
            "aggregation": {
                "alignment_period": {"seconds": 86400},
                "per_series_aligner": aligner
            },
            "view": monitoring_v3.ListTimeSeriesRequest.TimeSeriesView.FULL
        }
    )
    return results

def get_health_stats(project):
    all_counts  = {}
    true_counts  = {}
    output = {}
    results = get_metric(project, 'composer.googleapis.com/environment/healthy', monitoring_v3.Aggregation.Aligner.ALIGN_COUNT)
    for result in results:
        key = result.resource.labels['project_id'] + "," + result.resource.labels['location'] + "," + result.resource.labels['environment_name']
        value = result.points[0].value.int64_value
        all_counts[key] = value
    results= get_metric(project, 'composer.googleapis.com/environment/healthy', monitoring_v3.Aggregation.Aligner.ALIGN_COUNT_TRUE)

    for result in results:
        key = result.resource.labels['project_id'] + "," + result.resource.labels['location'] + "," + result.resource.labels['environment_name']
        value = result.points[0].value.int64_value
        true_counts[key] = value

    for all_entry_key in all_counts.keys():
        if all_counts[all_entry_key]!=0:
            output[all_entry_key] = round(100*true_counts[all_entry_key]/all_counts[all_entry_key])
    return output

def get_runs(project):
    all_runs = {}
    successful_runs = {}

    results = get_metric(project, 'composer.googleapis.com/workflow/run_count', monitoring_v3.Aggregation.Aligner.ALIGN_SUM)

    for result in results:
        key = result.resource.labels['project_id']+"," + result.resource.labels['location'] + "," + result.resource.labels['workflow_name'].split('.')[0]
        DAG = result.resource.labels['workflow_name'].split('.')[1]
        if DAG != "airflow_monitoring":
            if key not in all_runs:
                all_runs[key] = result.points[0].value.int64_value
            else:
                all_runs[key] += result.points[0].value.int64_value

            if result.metric.labels['state']=='success':
                if key not in successful_runs:
                    successful_runs[key] = result.points[0].value.int64_value
                else:
                    successful_runs[key] += result.points[0].value.int64_value
    
    output = {}
    for key in all_runs.keys():
        if all_runs[key] != 0:
            if key in successful_runs:
                output[key] = round(100*successful_runs[key]/all_runs[key])
            else:
                output[key] = 0

    return output

def print_envs(envs):
    output = "<table><tr><th>Project</th><th>Environment</th><th>Location</th><th>State</th><th>Composer<br>version</th><th>Airflow<br>version</th><th>Private IP</th><th>Created<p class='th_unit'>days ago</p></th><th>Updated<p class='th_unit'>days ago</p></th><th>Support<p class='th_unit'>months left</p></th><th>Health<p class='th_unit'>% of time last 24h</p></th><th>Successful DAG Runs<p class='th_unit'>% of all runs last 24h</p></th></tr>"
    
    for env in envs:
        state_f = "error" if env['state']=="ERROR" else "normal"
        composer_version_f = "warning" if env['composer_version'].startswith("1") else "normal"
        airflow_version_f = "normal" if env['airflow_version'].startswith("2.") else "warning" if env['airflow_version'] == "1.10.15" else "error"
        support_f = "error" if int(env['support']) <= 0 else "warning" if int(env['support']) <= 3 else "normal"
        private_f = "normal" if env['private'] == "Y" else "neutral"
        if env['health']!="":
            health_f = "error" if int(env['health']) <= 90 else "warning" if int(env['health']) < 100 else "normal"
        else:
            health_f = "error" 
        if env['dagsuccess']!="":
            dagsuccess_f = "error" if int(env['dagsuccess']) <= 90 else "warning" if int(env['dagsuccess']) < 100 else "normal"
        else:
            dagsuccess_f = "error" 

        output += "<tr><td>"+env['project'] + "</td><td>" + \
            "<a href='" + env['url'] + "'>" + env['environment'] + "</a></td>"+ \
            "<td class='neutral'>" + env['location'] + "</td>"+ \
            "<td class='" + state_f + "'>" + env['state']+ "</td>"+ \
            "<td class='" + composer_version_f + "'>" + env['composer_version'] + "</td>" + \
            "<td class='" + airflow_version_f + "'>" + env['airflow_version'] + "</td>"+ \
            "<td class='" + private_f + "'>" + env['private'] + "</td>"+ \
            "<td class='neutral'>" + env['created']+ "</td>"+ \
            "<td class='neutral'>" + env['updated'] + "</td>"+ \
            "<td class='" + support_f + "'>" + env['support']+ "</td>"+ \
            "<td class='" + health_f + "'>" + env['health']+ "</td>"+ \
            "<td class='" + dagsuccess_f + "'>" + env['dagsuccess']+ "</td>"+ \
            "</tr>"
    output += "</table>"
    return output

def print_errors(project_errors):
    output = ""
    if project_errors:
        output = "Errors found when trying to read data from the following projects:\n"
        for error in project_errors:
            output += str(error) + ", "
    return output

def generate_report(errors, envs, dashboard):
    output = "<html><body><head><style>body {background-color: #FFFFFF; font-family: Tahoma, sans-serif;}"
    output += "table {width: 100%;}"
    output += "h1 {font-weight: 400; font-size: 20px;}"
    output += "td {padding: 2px; text-align: left;}"
    output += "th {background-color: #DDDDFF;padding: 3px; text-align: center;font-size: 12px;}"
    output += "td {font-size: 12px;}"
    output += "p.refreshed {color: #888888; font-size: 12px;}"
    output += "p.heading {color: #333333; font-size: 16px;font-weight: 400;}"
    output += "p.th_unit {font-size: 12px;font-weight: 400;margin-block-start: 0em;margin-block-end: 0em;}"
    output += "table tr td.error {color: #bd1102; text-align: center;}"
    output += "table tr td.warning {color: #eda02b;text-align: center;}"
    output += "table tr td.normal {color: #1b9c02;text-align: center;}"
    output += "table tr td.neutral {color: black;text-align: center;}"
    output += ".topbar {overflow: hidden;background-color: #333;float: left;color: #FFFFFF;text-align: left;padding: 12px 12px;text-decoration: none;font-size: 17px;"
    output += "tr:nth-child(even) {background-color: #f2f2f2;} "
    output += ".button {background-color: #4066CE;border: none;font-weight: 300;color: white;padding: 15px;text-align: center;text-decoration: none;display: inline-block;font-size: 16px;margin: 4px 2px;cursor: pointer;border-radius: 4px;}"
    output += "</style></head>"
    output += "<div class='topbar'>Cloud Composer fleet manager</div>"

    now = datetime.datetime.now()
    formatted_time = now.strftime("%d/%m/%Y %H:%M:%S")
    output += "<p class='refreshed'>Refreshed on: " + formatted_time + " UTC</p>"
    output += "<p class='heading'>Environments' Monitoring Dashboard</p>"
    output += "<a href='" + dashboard + "'><button class='button'>Go to Monitoring Dashboard</button></a><br>"
    
    if errors:
        output += "<p style='color:red'>" + print_errors(errors) + "</p><br>"
    if envs:
        output += print_envs(envs)
    else:
        output += "No environments found<br><br>"

    output += "<p class='heading'>List of environments</p>"
    output += "</body></html>"
    return output

def save_report(bucket, obj, contents):
    print("Saving report...")
    print("bucket:"+bucket)
    print("obj:"+obj)
    print("content:"+contents)

    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket)

    blob = bucket.blob(obj)
    blob.upload_from_string(contents, content_type='text/html')

    return

@functions_framework.http
def fleetmon(request):
    months_of_support = 12                      # Months of support for each version
    q = Queue()

    projects_str = os.environ.get('PROJECTS')
    projects_str_clean = projects_str.replace(" ","")
    projects = projects_str_clean.split(",")

    project = os.environ.get('PROJECT_ID','')

    bucket = os.environ.get('BUCKET','')
    dashboard = os.environ.get('MONITORING_DASHBOARD','')

    try:
        regions = get_all_regions(project)
    except Exception as e:
        raise RuntimeError(e)

    try:
        versions = get_all_versions(project, months_of_support)
    except Exception as e:
        raise RuntimeError(e)

    envs = []
    project_errors = []

    threads = []
    to_remove = []
    env_added = False
    
    for project in projects:
        start = time.time()

        if env_added:
            for region in to_remove:
                if region in regions:
                    regions.remove(region)

        to_remove = []
        env_added = False

        try:
            health_metrics = get_health_stats(project)
        except Exception as e:
            print("ERROR: Error reading Cloud Monitoring metrics from project "+project+". Please add Monitoring Viewer role in project " + project + " to the service account used in this Function.")
            if project not in project_errors:
                project_errors.append(project)

        try:
            runs_metrics = get_runs(project)
        except Exception as e:
            print("ERROR: Error reading Cloud Monitoring metrics from project "+project+". Please add Monitoring Viewer role in project " + project + " to the service account used in this Function.")
            if project not in project_errors:
                project_errors.append(project)

        for region in regions:
            threads.append(Thread(target = list_envs, args =(project, region, versions, q )))
            threads[-1].start()

        for thread in threads:
            thread.join(timeout=5)

        while not q.empty():
            result = q.get()
            if 'error' in result:
                if result['project'] not in project_errors:
                    project_errors.append(result['project'])
            else:    
                if result['to_remove']:
                    to_remove.append(result['to_remove'])
                    env_added = True
                if len(result['envs'])>0:
                    for env in result['envs']:
                        key = env['project'] + "," + env['location'] + "," + env['environment']
                        if key in health_metrics:
                            env['health'] = str(health_metrics[key])
                        else:
                            env['health'] = "0"
                       
                        if key in runs_metrics:
                            env['dagsuccess'] = str(runs_metrics[key])
                        else:
                            env['dagsuccess'] = ""
                        envs.append(env)       

    contents = generate_report(project_errors, envs, dashboard)

    try:
        save_report(bucket,"report.html", contents)
    except Exception as e:
        return "Error saving a report: " + str(e)

    return "Report regenerated"