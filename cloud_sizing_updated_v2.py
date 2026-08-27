#!/usr/bin/env python3
"""
Cortex Cloud sizing calculator.

Counts billable workloads (SKU) across AWS, Azure, GCP and OCI.

GCP supports three collection modes (see --gcp-mode):
  project  per-project API calls (original behaviour, now parallel + resumable)
  asset    one org/folder-wide Cloud Asset Inventory read  -- creates NOTHING
  bq       Cloud Asset Inventory export to an EPHEMERAL BigQuery dataset that
           this script creates and deletes itself (see --gcp-mode bq notes)
"""

import argparse
import atexit
import csv
import json
import logging
import math
import os
import re
import signal
import sys
import threading
import warnings
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

# Supprimer les avertissements de dépréciation
warnings.filterwarnings('ignore', category=DeprecationWarning)

# gRPC logs "Other threads are currently calling into gRPC, skipping fork()
# handlers" and "FD from fork parent still in poll list" whenever a threaded
# client is used, which is exactly what --workers does. Harmless, but it drowns
# the real output. Must be set before any google.cloud import.
os.environ.setdefault("GRPC_VERBOSITY", "NONE")
os.environ.setdefault("GRPC_ENABLE_FORK_SUPPORT", "0")
os.environ.setdefault("ABSL_LOGGING_MIN_LOG_LEVEL", "3")

# "httplib2 transport does not support per-request timeout" is emitted once per
# request by google_auth_httplib2 (NOT by googleapiclient, despite the wording).
# Four per project is 180 000 lines on a large estate, and it says nothing
# actionable: real API problems go through record_api_failure() instead.
# The emitting module has moved between releases, so silence the whole family
# rather than betting on one name.
for _noisy in ("google_auth_httplib2", "googleapiclient", "googleapiclient.discovery_cache",
               "oauth2client", "httplib2", "google.auth.transport.requests"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

# Belt and braces: nothing configures logging here, so any WARNING from a
# library we did not name above reaches stderr through logging.lastResort and
# lands in the middle of the report. Drop that one message wherever it fires.
class _DropPerRequestTimeout(logging.Filter):
    def filter(self, record):
        return "does not support per-request timeout" not in record.getMessage()

logging.lastResort.addFilter(_DropPerRequestTimeout())

parser = argparse.ArgumentParser(
    formatter_class=argparse.RawDescriptionHelpFormatter,
    epilog="""
Large GCP organisations (thousands of projects)
-----------------------------------------------
  python3 cloud_sizing_updated_v2.py --gcp --gcp-scope organizations/123456789 \\
      --output csv --csv-file sizing.csv

  Uses Cloud Asset Inventory. Read-only, creates no resource in the tenant.
  Needs: roles/cloudasset.viewer on the scope, and cloudasset.googleapis.com
  enabled on the quota project (an API enablement, not a resource).
""")
parser.add_argument("--azure", "-az", help="Sizing for Azure", action='store_true')
parser.add_argument("--aws", "-a", help="Sizing for AWS", action='store_true')
parser.add_argument("--gcp", "-g", help="Sizing for GCP", action='store_true')
parser.add_argument("--oci", "-o", help="Sizing for OCI", action='store_true')
parser.add_argument("--region-prefix", "-rp", help="Filter AWS regions by prefix (e.g. us, eu, ap)", default=None)
parser.add_argument("--output", "-out", help="Output format", default="table", choices=["table", "json", "csv"])
parser.add_argument("--csv-file", help="Write the full per-account CSV here (default: stdout)", default=None)
parser.add_argument("--details", help="Print the per-account breakdown even on large estates", action='store_true')
parser.add_argument("--top", help="Rows shown in the table summary (0 = all)", type=int, default=25)
parser.add_argument("--workers", "-w", help="Parallel workers for scans", type=int, default=16)
parser.add_argument("--count-images", help="Count container registry images (slow, informational only)",
                    action='store_true')
parser.add_argument("--yes", "-y", help="Answer yes to every prompt (unattended runs)", action='store_true')
parser.add_argument("--check", help="Run the preflight checks and stop, without scanning", action='store_true')
parser.add_argument("--no-bootstrap", help="Never create a venv or install anything", action='store_true')

gcp_group = parser.add_argument_group("GCP")
gcp_group.add_argument("--gcp-mode", choices=["auto", "asset", "project", "bq"], default="auto",
                       help="Collection mode. auto = asset if --gcp-scope is an org/folder, else project")
gcp_group.add_argument("--gcp-scope", help="organizations/N, folders/N, projects/ID (a bare number = organization)")
gcp_group.add_argument("--checkpoint", help="Resumable checkpoint file (--gcp-mode project)")
gcp_group.add_argument("--bq-project", help="Project hosting the EPHEMERAL dataset (--gcp-mode bq)")
gcp_group.add_argument("--bq-location", default="US", help="Location of the ephemeral dataset (--gcp-mode bq)")
gcp_group.add_argument("--bq-cleanup-only", action='store_true',
                       help="Delete leftover ephemeral datasets from a previous interrupted run, then exit")

args = parser.parse_args()
separator = "-" * 100


def warn(msg):
    """Diagnostics go to stderr so that --output json/csv stays machine-readable."""
    sys.stdout.flush()  # keep the two streams interleaved in order when piped
    print(msg, file=sys.stderr, flush=True)


# --- Aggregated API failures -------------------------------------------------
# Per-project mode hits the same wall in every project: an API disabled, or a
# missing role. Printing the raw exception per project per service means 8 x N
# protobuf dumps, which buries the result and hides the fact that the counts are
# incomplete. Collect instead, and print one line per (service, root cause).
GCP_SERVICE_API = {
    "Compute Engine": "compute.googleapis.com",
    "Kubernetes Engine": "container.googleapis.com",
    "Cloud Functions": "cloudfunctions.googleapis.com",
    "Cloud Run": "run.googleapis.com",
    "Cloud Storage": "storage.googleapis.com",
    "BigQuery": "bigquery.googleapis.com",
    "Bigtable": "bigtableadmin.googleapis.com",
    "Cloud SQL": "sqladmin.googleapis.com",
}
# "...has not been used in project X before or it is disabled". X is not always the
# scanned project: admin APIs bill the quota project, so that is the one to enable.
_DISABLED_RE = re.compile(r"has not been used in project ([A-Za-z0-9:.\-]+) before")
_API_FAILURES = defaultdict(lambda: {"disabled": defaultdict(set), "denied": set(), "other": {}})
_API_FAILURES_LOCK = threading.Lock()


def record_api_failure(service, project_id, exc):
    """Bucket one failure by root cause instead of printing it."""
    text = str(exc)
    match = _DISABLED_RE.search(text)
    with _API_FAILURES_LOCK:
        bucket = _API_FAILURES[service]
        if match or "SERVICE_DISABLED" in text:
            bucket["disabled"][match.group(1) if match else project_id].add(project_id)
        elif ("PERMISSION_DENIED" in text or "denied on resource" in text
              or "not authorized" in text.lower() or "permission" in text.lower()):
            bucket["denied"].add(project_id)
        else:
            bucket["other"].setdefault(text.strip().splitlines()[0][:150], set()).add(project_id)


def print_api_failure_summary(total_projects):
    """Tell the operator what was NOT counted. Silence here would inflate confidence."""
    if not _API_FAILURES:
        return
    warn(f"\n{separator}")
    warn("PARTIAL COVERAGE - the counts below are a FLOOR, not a total")
    warn(separator)
    def sample(names):
        names = sorted(names)
        return ", ".join(names[:3]) + (f", +{len(names) - 3} more" if len(names) > 3 else "")

    for service in sorted(_API_FAILURES):
        bucket = _API_FAILURES[service]
        api = GCP_SERVICE_API.get(service, service)
        # An admin API bills the quota project, so "enable on" is sometimes a single
        # shared project rather than each scanned one. Those two cases read very
        # differently: one command to run vs one command per project.
        own, shared = set(), {}
        for enable_on, projects in bucket["disabled"].items():
            if len(projects) == 1 and enable_on in projects:
                own |= projects          # the scanned project is its own target
            else:
                shared.setdefault(enable_on, set()).update(projects)
        for enable_on, projects in sorted(shared.items()):
            warn(f"  {service:<18} API disabled       {len(projects):>5}/{total_projects} project(s) not counted")
            warn(f"  {'':<18} gcloud services enable {api} --project {enable_on}")
        if own:
            warn(f"  {service:<18} API disabled       {len(own):>5}/{total_projects} project(s) not counted")
            warn(f"  {'':<18} enable {api} on each: {sample(own)}")
        if bucket["denied"]:
            warn(f"  {service:<18} permission denied  {len(bucket['denied']):>5}/{total_projects} project(s) not counted")
            warn(f"  {'':<18} e.g. {sample(bucket['denied'])}")
        for msg, projects in bucket["other"].items():
            warn(f"  {service:<18} {len(projects):>5}/{total_projects} project(s): {msg}")
    warn(separator)
    warn("  Cloud Asset Inventory reports assets even where these APIs are disabled and")
    warn("  needs no per-project grant. With roles/cloudasset.viewer on the org, re-run:")
    warn("      --gcp-scope organizations/<ID>")
    warn(separator)


# New licensing metrics based on the image
cc_metering = {
    "vm_no_containers": 1,           # VMs not running containers: 1 VM
    "vm_with_containers": 1,          # VMs running containers: 1 VM
    "caas": 10,                       # CaaS: 10 Managed Containers
    "serverless": 25,                 # Serverless Functions: 25 Functions
    "buckets": 10,                    # Cloud Buckets: 10 Buckets
    "paas_db": 2,                     # Managed Cloud Database (PaaS): 2 PaaS Databases
    "dbaas_tb": 1,                    # DBaaS TB stored: 1 TB (not used in current calculation)
    "saas_users": 10,                 # SaaS users: 10 SaaS Users
    "asm": 4,                         # Cloud ASM - service: 4 Unmanaged Assets
    "container_images": 10            # Container Images in Registries: 10 scans (free quota per VM/CaaS)
}

cc_metering_table = [
    ["VMs not running containers", "1 VM"],
    ["VMs running containers", "1 VM"],
    ["CaaS", "10 Managed Containers"],
    ["Serverless Functions", "25 Serverless Functions"],
    ["Cloud Buckets", "10 Cloud Buckets"],
    ["Managed Cloud Database (PaaS)", "2 PaaS Databases"],
    ["DBaaS TB stored", "1 TB Stored"],
    ["SaaS users", "10 SaaS Users"],
    ["Cloud ASM - service", "4 Unmanaged Assets"],
    ["Container Images in Registries", "Free quota: 10 container image scans per deployed workload (VM/CaaS)"]
]


def cortex_cloud_metering():
    if args.output != "table":
        return
    print(f"\n{separator}\nCortex Cloud Workload Metering\n{separator}")
    print(f"{'Workload Type':<45} {'Billable Units':<50}\n{separator}")
    for workload, units in cc_metering_table:
        print(f"{workload:<45} {units:<50}")
    print(separator)


def tables(account_info, data):
    if args.output != "table" or not args.details:
        return
    print(f"{'Account':<50} {'Service':<40} {'Count':<10}\n{separator}")
    account = f'{account_info["Id"]} ({account_info["Name"]})' if account_info else ""
    for a, b in data:
        print(f"{account:<50} {a:<40} {b:<10}")
    print(separator)


def licensing_count(cloud, vm_no_containers, vm_with_containers, caas, serverless, buckets, paas_db,
                    container_images=0, account_info=None):
    """
    Calculate the number of workloads (SKU) required based on new metrics

    Note: Container images have a free quota of 10 scans per deployed workload (VM/CaaS)
    Calculation of images beyond the free quota is not implemented here.
    container_images may be None, meaning "not collected" (never a fabricated estimate).
    """
    # Calculate workloads required for each resource type
    vm_no_cont_workloads = math.ceil(vm_no_containers / cc_metering["vm_no_containers"])
    vm_with_cont_workloads = math.ceil(vm_with_containers / cc_metering["vm_with_containers"])
    caas_workloads = math.ceil(caas / cc_metering["caas"])
    serverless_workloads = math.ceil(serverless / cc_metering["serverless"])
    buckets_workloads = math.ceil(buckets / cc_metering["buckets"])
    paas_db_workloads = math.ceil(paas_db / cc_metering["paas_db"])

    # Total workloads
    total = (
        vm_no_cont_workloads +
        vm_with_cont_workloads +
        caas_workloads +
        serverless_workloads +
        buckets_workloads +
        paas_db_workloads
    )

    if args.output == "table" and args.details:
        print(f"\n--- Licensing Breakdown ---")
        print(f"VMs (no containers): {vm_no_containers} → {vm_no_cont_workloads} workload(s)")
        print(f"VMs (with containers): {vm_with_containers} → {vm_with_cont_workloads} workload(s)")
        print(f"CaaS: {caas} → {caas_workloads} workload(s)")
        print(f"Serverless: {serverless} → {serverless_workloads} workload(s)")
        print(f"Cloud Buckets: {buckets} → {buckets_workloads} workload(s)")
        print(f"PaaS Databases: {paas_db} → {paas_db_workloads} workload(s)")

        # Container images info (free quota)
        total_deployed_workloads = vm_no_containers + vm_with_containers + caas
        free_image_scans = total_deployed_workloads * 10
        shown = "not collected (use --count-images)" if container_images is None else container_images
        print(f"\nContainer Images: {shown} (Free quota: {free_image_scans} scans)")

        print(f"\n{'=' * 60}")
        print(f"TOTAL: {total} Cortex Cloud workload(s) (SKU) needed for {cloud}")
        print(f"{'=' * 60}\n{separator}")

    return {
        "cloud": cloud,
        "account_id": account_info["Id"] if account_info else "N/A",
        "account_name": account_info["Name"] if account_info else "N/A",
        "vm_no_containers": vm_no_containers,
        "vm_with_containers": vm_with_containers,
        "caas": caas,
        "serverless": serverless,
        "buckets": buckets,
        "paas_db": paas_db,
        "container_images": container_images,
        "total_workloads": total,
        "breakdown": {
            "vm_no_cont_workloads": vm_no_cont_workloads,
            "vm_with_cont_workloads": vm_with_cont_workloads,
            "caas_workloads": caas_workloads,
            "serverless_workloads": serverless_workloads,
            "buckets_workloads": buckets_workloads,
            "paas_db_workloads": paas_db_workloads
        }
    }


def _rank_key(row):
    """Biggest first, then by id so two runs of the same estate produce identical files."""
    return (-row["total_workloads"], str(row["account_id"]))


CSV_COLUMNS = ["cloud", "account_id", "account_name", "vm_no_containers", "vm_with_containers",
               "caas", "serverless", "buckets", "paas_db", "container_images", "total_workloads"]


def write_csv(results):
    handle = open(args.csv_file, "w", newline="", encoding="utf-8") if args.csv_file else sys.stdout
    try:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for r in sorted(results, key=_rank_key):
            writer.writerow(r)
    finally:
        if args.csv_file:
            handle.close()
            warn(f"Wrote {len(results)} rows to {args.csv_file}")


def print_global_summary(results):
    """
    Display a global summary table with the total SKUs per account/subscription/project.
    On large estates only the top N rows are printed; the grand total always covers everything.
    """
    if not results:
        print("\nNo account/subscription/project returned any data.")
        return 0

    ranked = sorted(results, key=_rank_key)
    limit = len(ranked) if args.top <= 0 else min(args.top, len(ranked))
    empty = sum(1 for r in ranked if r["total_workloads"] == 0)

    print(f"\n\n{'=' * 120}")
    print(f"{'GLOBAL SKU SUMMARY - ALL ACCOUNTS/SUBSCRIPTIONS/PROJECTS':^120}")
    print(f"{'=' * 120}")
    if limit < len(ranked):
        print(f"{'(top ' + str(limit) + ' of ' + str(len(ranked)) + ' by SKU -- use --top 0 or --output csv for all)':^120}")
    print(f"{'Cloud':<15} {'Account/Subscription/Project ID':<45} {'Account Name':<35} {'SKU':<10}")
    print(f"{'-' * 120}")

    total_sku = 0
    cloud_totals = {}

    for result in ranked:
        cloud = result['cloud']
        sku = result['total_workloads']
        total_sku += sku
        cloud_totals[cloud] = cloud_totals.get(cloud, 0) + sku

    for result in ranked[:limit]:
        account_name = result['account_name'] or ""
        # Truncate names that are too long
        account_name_display = (account_name[:32] + '...') if len(account_name) > 35 else account_name
        print(f"{result['cloud']:<15} {result['account_id']:<45} {account_name_display:<35} "
              f"{result['total_workloads']:<10}")

    if limit < len(ranked):
        remainder = sum(r["total_workloads"] for r in ranked[limit:])
        print(f"{'...':<15} {str(len(ranked) - limit) + ' more accounts':<45} {'':<35} {remainder:<10}")
    print(f"{'-' * 120}")

    # Subtotals by cloud
    if len(cloud_totals) > 1:
        print(f"\n{'SUBTOTALS BY CLOUD PROVIDER':^120}")
        print(f"{'-' * 120}")
        for cloud, subtotal in cloud_totals.items():
            print(f"{cloud:<15} {'Subtotal':<80} {subtotal:<10}")
        print(f"{'-' * 120}")

    print(f"\n{'Accounts scanned':<95} {len(ranked):<10}")
    print(f"{'Accounts with 0 SKU':<95} {empty:<10}")
    if _API_FAILURES:
        services = ", ".join(sorted(_API_FAILURES))
        print(f"{'GRAND TOTAL (INCOMPLETE - see PARTIAL COVERAGE above)':<95} {total_sku:<10}")
        print(f"{'  not fully readable: ' + services:<95}")
    else:
        print(f"{'GRAND TOTAL':<95} {total_sku:<10}")
    print(f"{'=' * 120}\n")

    return total_sku


def emit(results):
    """Single exit point for every provider."""
    # --csv-file always produces the full file, whatever is rendered on screen,
    # so a run can show the summary AND leave a deliverable behind.
    if args.csv_file:
        write_csv(results)
    if args.output == "json":
        print(json.dumps(results, indent=2))
    elif args.output == "csv":
        if not args.csv_file:
            write_csv(results)
    else:
        print_global_summary(results)


# ---------------------------- AWS ----------------------------
def _boto_config():
    from botocore.config import Config
    # Adaptive retries absorb the throttling that shows up on large organisations.
    return Config(retries={"max_attempts": 10, "mode": "adaptive"})


def _paginated_count(client, operation, result_key, **kwargs):
    """Count every item of a paginated boto3 operation (the v1 script only read page 1)."""
    total = 0
    paginator = client.get_paginator(operation)
    for page in paginator.paginate(**kwargs):
        total += len(page.get(result_key, []))
    return total


def _aws_region_scan(session, region, client_lock):
    """Collect one region. Returns a dict of counters."""
    import botocore

    out = defaultdict(int)

    def client(name):
        with client_lock:  # boto3 client construction is not thread-safe
            return session.client(name, region_name=region, config=_boto_config())

    try:
        ec2 = client('ec2')
        # State codes: 16 = running, 80 = stopped
        # Ne pas inclure: 0 (pending), 32 (shutting-down), 48 (terminated), 64 (stopping)
        for page in ec2.get_paginator('describe_instances').paginate(
                Filters=[{'Name': 'instance-state-code', 'Values': ["16", "80"]}]):
            for reservation in page['Reservations']:
                for instance in reservation['Instances']:
                    tags = instance.get('Tags', [])
                    if any("eks:" in tag.get("Key", "") for tag in tags):
                        out['eks'] += 1
                    else:
                        out['ec2'] += 1
    except botocore.exceptions.ClientError as error:
        warn(f"EC2 error in {region}: {error}")

    # ECS Tasks (managed containers)
    try:
        ecs_client = client('ecs')
        for page in ecs_client.get_paginator('list_clusters').paginate():
            for cluster in page['clusterArns']:
                out['ecs'] += _paginated_count(ecs_client, 'list_tasks', 'taskArns',
                                               cluster=cluster, desiredStatus='RUNNING')
        # Fargate task definitions (informational: definitions, not running tasks)
        out['fargate'] += _paginated_count(ecs_client, 'list_task_definitions', 'taskDefinitionArns')
    except botocore.exceptions.ClientError as error:
        warn(f"ECS error in {region}: {error}")

    for label, service, operation, key in (
        ('lambdas', 'lambda', 'list_functions', 'Functions'),
        ('rds', 'rds', 'describe_db_instances', 'DBInstances'),
        ('dynamodb', 'dynamodb', 'list_tables', 'TableNames'),
        ('efs', 'efs', 'describe_file_systems', 'FileSystems'),
    ):
        try:
            out[label] += _paginated_count(client(service), operation, key)
        except botocore.exceptions.ClientError as error:
            warn(f"{service} error in {region}: {error}")

    return out


def aws(account, session=None):
    import boto3
    import botocore

    if session is None:
        session = boto3.Session()
    client_lock = threading.Lock()

    try:
        regions = [r['RegionName'] for r in session.client('ec2', config=_boto_config())
                   .describe_regions()['Regions']]
        if args.region_prefix:
            regions = [r for r in regions if r.startswith(args.region_prefix)]
    except botocore.exceptions.ClientError as error:
        raise error

    totals = defaultdict(int)

    # ---------------- S3 Buckets (global) ----------------
    try:
        s3 = session.client('s3', config=_boto_config())
        totals['s3'] = len(s3.list_buckets()['Buckets'])
    except botocore.exceptions.ClientError as error:
        warn(f"S3 error: {error}")

    # ---------------- ECR Images (global, opt-in: expensive) ----------------
    ecr_images = None
    if args.count_images:
        ecr_images = 0
        try:
            ecr = session.client('ecr', config=_boto_config())
            for page in ecr.get_paginator('describe_repositories').paginate():
                for repo in page['repositories']:
                    try:
                        ecr_images += _paginated_count(ecr, 'list_images', 'imageIds',
                                                       repositoryName=repo['repositoryName'])
                    except botocore.exceptions.ClientError as error:
                        warn(f"ECR error on {repo['repositoryName']}: {error}")
        except botocore.exceptions.ClientError as error:
            warn(f"ECR error: {error}")

    # ---------------- Regional Services (parallel) ----------------
    with ThreadPoolExecutor(max_workers=min(args.workers, max(1, len(regions)))) as pool:
        futures = {pool.submit(_aws_region_scan, session, r, client_lock): r for r in regions}
        for future in as_completed(futures):
            try:
                for key, value in future.result().items():
                    totals[key] += value
            except Exception as error:  # never lose the whole account over one region
                warn(f"Region {futures[future]} failed: {error}")

    # Calculation: EC2 without containers vs EKS (with containers)
    vm_no_containers = totals['ec2']
    vm_with_containers = totals['eks']
    caas = totals['ecs']  # ECS running tasks
    serverless = totals['lambdas']
    buckets = totals['s3']
    paas_db = totals['rds'] + totals['dynamodb'] + totals['efs']

    tables(account, [
        ["EC2 Instances (no containers)", vm_no_containers],
        ["EKS Nodes (with containers)", vm_with_containers],
        ["ECS Tasks (CaaS)", caas],
        ["Fargate Task Definitions", totals['fargate']],
        ["Lambda Functions", serverless],
        ["S3 Buckets", buckets],
        ["RDS Instances", totals['rds']],
        ["DynamoDB Tables", totals['dynamodb']],
        ["EFS Systems", totals['efs']],
        ["ECR Container Images", "not collected" if ecr_images is None else ecr_images]
    ])

    return licensing_count("AWS", vm_no_containers, vm_with_containers, caas, serverless, buckets, paas_db,
                           ecr_images, account)


def _aws_scan_member(account):
    import boto3
    import botocore
    role_arn = f"arn:aws:iam::{account['Id']}:role/OrganizationAccountAccessRole"
    try:
        creds = boto3.client('sts', config=_boto_config()).assume_role(
            RoleArn=role_arn, RoleSessionName='CrossAccountSession'
        )['Credentials']
        session = boto3.Session(
            aws_access_key_id=creds['AccessKeyId'],
            aws_secret_access_key=creds['SecretAccessKey'],
            aws_session_token=creds['SessionToken']
        )
        return aws({"Name": account['Name'], "Id": account['Id']}, session=session)
    except botocore.exceptions.ClientError as error:
        warn(f"Error with {account['Name']} - {account['Id']}:\n{error}\n{separator}")
        return None


def pcs_sizing_aws():
    import boto3
    import botocore

    sts = boto3.client("sts", config=_boto_config())
    iam = boto3.client('iam', config=_boto_config())
    org = boto3.client('organizations', config=_boto_config())
    accounts = []
    results = []

    aliases = iam.list_account_aliases().get('AccountAliases', [])
    caller_account = sts.get_caller_identity()["Account"]
    account_info = {"Name": aliases[0] if aliases else 'No alias', "Id": caller_account}
    results.append(aws(account_info))

    try:
        paginator = org.get_paginator('list_accounts')
        for page in paginator.paginate():
            for acct in page['Accounts']:
                # Skip the payer account: it was already scanned with the ambient credentials.
                if acct['Status'] == "ACTIVE" and acct['Id'] != caller_account:
                    accounts.append(acct)
    except botocore.exceptions.ClientError as error:
        warn(f"{error}\n{separator}")

    if accounts:
        warn(f"Scanning {len(accounts)} member accounts with {args.workers} workers...")
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for result in pool.map(_aws_scan_member, accounts):
                if result:
                    results.append(result)

    emit(results)


# ---------------------------- Azure ----------------------------
def _azure_is_aks_node(vm):
    """AKS node VMs must not be counted twice (once as VM, once as AKS node)."""
    resource_group = vm.id.split('/')[4] if vm.id else ""
    if resource_group.upper().startswith("MC_"):
        return True
    tags = {k.lower() for k in (vm.tags or {})}
    return any(t.startswith("aks-managed") or t == "orchestrator" for t in tags)


def pcs_sizing_az():
    import re
    from azure.mgmt.compute import ComputeManagementClient
    from azure.identity import DefaultAzureCredential
    from azure.mgmt.containerservice import ContainerServiceClient
    from azure.mgmt.subscription import SubscriptionClient
    from azure.mgmt.web import WebSiteManagementClient
    from azure.mgmt.sql import SqlManagementClient
    from azure.mgmt.cosmosdb import CosmosDBManagementClient
    from azure.mgmt.storage import StorageManagementClient
    from azure.mgmt.containerregistry import ContainerRegistryManagementClient

    credential = DefaultAzureCredential()
    sub_client = SubscriptionClient(credential)
    results = []

    warn(f"\n{separator}\nGetting Resources from AZURE\n{separator}")

    for sub in sub_client.subscriptions.list():
        sub_id = sub.subscription_id
        sub_name = sub.display_name

        compute_client = ComputeManagementClient(credential, sub_id)
        containerservice_client = ContainerServiceClient(credential, sub_id)
        app_service_client = WebSiteManagementClient(credential, sub_id)
        sql_client = SqlManagementClient(credential, sub_id)
        cosmos_client = CosmosDBManagementClient(credential, sub_id)
        storage_client = StorageManagementClient(credential, sub_id)
        acr_client = ContainerRegistryManagementClient(credential, sub_id)

        # ------------------- VMs (Running et Stopped uniquement) -------------------
        # status_only='true' returns the power state inline: one call instead of one
        # instance_view round-trip per VM (the v1 bottleneck on large subscriptions).
        vm_count = 0
        try:
            vms = compute_client.virtual_machines.list_all(status_only='true')
            inline_status = True
        except TypeError:  # older azure-mgmt-compute without status_only
            vms = compute_client.virtual_machines.list_all()
            inline_status = False

        for vm in vms:
            try:
                if _azure_is_aks_node(vm):
                    continue  # counted via AKS agent pools below
                statuses = (vm.instance_view.statuses if inline_status and vm.instance_view else None)
                if statuses is None:
                    statuses = compute_client.virtual_machines.instance_view(
                        vm.id.split('/')[4], vm.name
                    ).statuses
                # Ne compter que les VMs en état running ou stopped
                # États exclus: deallocated, deallocating, starting, stopping, unknown
                if any('PowerState/running' in s.code or 'PowerState/stopped' in s.code for s in statuses):
                    vm_count += 1
            except Exception as e:
                warn(f"VM error for {vm.name}: {e}")
                continue

        # ------------------- AKS Nodes -------------------
        node_count = 0
        for cl in containerservice_client.managed_clusters.list():
            try:
                for ap in containerservice_client.agent_pools.list(cl.id.split('/')[4], cl.name):
                    node_count += ap.count or 0
            except Exception as e:
                warn(f"AKS error: {e}")
                continue

        # ------------------- Azure Container Instances (ACI) -------------------
        # Note: ACI requires azure.mgmt.containerinstance - not included here
        aci_count = 0

        # ------------------- Azure Functions -------------------
        function_list = [
            f.name for f in app_service_client.web_apps.list()
            if f.kind and f.kind.startswith('function')
        ]

        # ------------------- SQL Databases -------------------
        sql_db_count = 0
        for s in sql_client.servers.list():
            try:
                match = re.search(r"/resourceGroups/([^/]+)/", s.id)
                if match:
                    rg_name = match.group(1)
                    sql_db_count += len(list(sql_client.databases.list_by_server(rg_name, s.name)))
            except Exception as e:
                warn(f"SQL error: {e}")
                continue

        # ------------------- Cosmos DB -------------------
        cosmos_count = sum(
            1 for acc in cosmos_client.database_accounts.list()
            if getattr(acc, "public_network_access", None) == "Enabled"
        )

        # ------------------- Storage Accounts -------------------
        storage_count = sum(1 for _ in storage_client.storage_accounts.list())

        # ------------------- Container Registries -------------------
        # Image counting needs the Docker Registry v2 data plane, which this script
        # does not talk to. We report registries and leave images uncollected rather
        # than inventing a number.
        acr_registries = 0
        try:
            acr_registries = sum(1 for _ in acr_client.registries.list())
        except Exception as e:
            warn(f"ACR error: {e}")

        # Metrics calculation
        vm_no_containers = vm_count
        vm_with_containers = node_count
        caas = aci_count  # Azure Container Instances
        serverless = len(function_list)
        buckets = storage_count
        paas_db = cosmos_count + sql_db_count

        account_info = {"Name": sub_name, "Id": sub_id}
        tables(account_info, [
            ["VM (no containers)", vm_no_containers],
            ["AKS Nodes (with containers)", vm_with_containers],
            ["Azure Container Instances", aci_count],
            ["Azure Functions", serverless],
            ["Azure SQL Databases", sql_db_count],
            ["Cosmos DB", cosmos_count],
            ["Storage Accounts", storage_count],
            ["Container Registries", acr_registries]
        ])

        results.append(licensing_count(
            "Azure", vm_no_containers, vm_with_containers, caas, serverless, buckets, paas_db,
            None, account_info
        ))

    emit(results)


# ---------------------------- GCP ----------------------------
# Asset types needed for the licensing metrics. Keep in sync with _gcp_classify().
GCP_ASSET_TYPES = [
    "cloudresourcemanager.googleapis.com/Project",   # project id / display name lookup
    "compute.googleapis.com/Instance",
    "container.googleapis.com/Cluster",              # informational
    "run.googleapis.com/Service",
    "cloudfunctions.googleapis.com/CloudFunction",
    "cloudfunctions.googleapis.com/Function",
    "storage.googleapis.com/Bucket",
    "bigquery.googleapis.com/Dataset",
    "bigtableadmin.googleapis.com/Instance",
    "sqladmin.googleapis.com/Instance",
]
GCP_IMAGE_ASSET_TYPE = "artifactregistry.googleapis.com/DockerImage"

# Cloud SQL states that must NOT be billed. Anything else counts: for a sizing
# exercise an unknown state should over-count, never silently disappear.
_SQL_INACTIVE_STATES = {"STOPPED", "SUSPENDED", "PENDING_DELETE", "FAILED", "MAINTENANCE"}


def _gcp_classify(asset_type, state, labels):
    """Map one asset to a licensing counter, or None if it must not be counted."""
    if asset_type == "compute.googleapis.com/Instance":
        if state not in ("RUNNING", "TERMINATED"):  # TERMINATED = stopped dans GCP
            return None
        # GKE nodes are Compute instances too. v1 counted them twice: once here and
        # once via currentNodeCount. The goog-gke-node label is what separates them.
        return "vm_with_containers" if "goog-gke-node" in labels else "vm_no_containers"
    if asset_type == "run.googleapis.com/Service":
        # Cloud Functions gen2 run on Cloud Run; they are already counted as serverless.
        if labels.get("goog-managed-by") == "cloudfunctions":
            return None
        return "caas"
    if asset_type in ("cloudfunctions.googleapis.com/CloudFunction",
                      "cloudfunctions.googleapis.com/Function"):
        return "serverless"
    if asset_type == "storage.googleapis.com/Bucket":
        return "buckets"
    if asset_type in ("bigquery.googleapis.com/Dataset", "bigtableadmin.googleapis.com/Instance"):
        return "paas_db"
    if asset_type == "sqladmin.googleapis.com/Instance":
        return None if state.upper() in _SQL_INACTIVE_STATES else "paas_db"
    if asset_type == "container.googleapis.com/Cluster":
        return "gke_clusters"
    if asset_type == GCP_IMAGE_ASSET_TYPE:
        return "container_images"
    return None


def _normalize_scope(scope):
    if not scope:
        return None
    scope = scope.strip()
    if scope.isdigit():
        return f"organizations/{scope}"
    return scope


def _gcp_results_from_counts(counts, project_meta):
    """Turn {project_number: {counter: n}} into the standard result rows."""
    results = []
    for project_key, c in counts.items():
        meta = project_meta.get(project_key, {})
        account_info = {
            "Id": meta.get("project_id") or project_key,
            "Name": meta.get("display_name") or "",
        }
        tables(account_info, [
            ["Compute Instances (no containers)", c["vm_no_containers"]],
            ["GKE Nodes (with containers)", c["vm_with_containers"]],
            ["GKE Clusters", c["gke_clusters"]],
            ["Google Functions", c["serverless"]],
            ["Google CloudRun (CaaS)", c["caas"]],
            ["Cloud Storage Buckets", c["buckets"]],
            ["PaaS Databases (BQ/Bigtable/CloudSQL)", c["paas_db"]],
        ])
        results.append(licensing_count(
            "GCP", c["vm_no_containers"], c["vm_with_containers"], c["caas"], c["serverless"],
            c["buckets"], c["paas_db"],
            c["container_images"] if args.count_images else None,
            account_info
        ))
    return results


def _new_gcp_counts():
    return defaultdict(int, {k: 0 for k in (
        "vm_no_containers", "vm_with_containers", "caas", "serverless",
        "buckets", "paas_db", "gke_clusters", "container_images")})


def _gcp_scan_asset_inventory(scope):
    """
    Org/folder-wide read through Cloud Asset Inventory.

    Creates NOTHING in the customer tenant: searchAllResources is a read-only API.
    One request stream per asset type, run in parallel, so a failure on one type
    (API not enabled, permission gap) never kills the whole scan.
    """
    from google.cloud import asset_v1
    from google.api_core import exceptions as core_exceptions
    from google.api_core import retry as core_retry

    client = asset_v1.AssetServiceClient()
    retry = core_retry.Retry(
        predicate=core_retry.if_exception_type(
            core_exceptions.ResourceExhausted,     # quota / 429
            core_exceptions.ServiceUnavailable,
            core_exceptions.DeadlineExceeded,
            core_exceptions.InternalServerError,
            core_exceptions.Aborted,
        ),
        initial=2.0, maximum=60.0, multiplier=2.0, timeout=1800.0,
    )

    asset_types = list(GCP_ASSET_TYPES)
    if args.count_images:
        asset_types.append(GCP_IMAGE_ASSET_TYPE)

    counts = defaultdict(_new_gcp_counts)
    project_meta = {}
    lock = threading.Lock()

    def scan_type(asset_type):
        local_counts = defaultdict(_new_gcp_counts)
        local_meta = {}
        seen = 0
        request = asset_v1.SearchAllResourcesRequest(
            scope=scope, asset_types=[asset_type], page_size=500)
        for item in client.search_all_resources(request=request, retry=retry, timeout=180.0):
            seen += 1
            project_key = (item.project or "").rsplit("/", 1)[-1]
            if asset_type == "cloudresourcemanager.googleapis.com/Project":
                extra = dict(item.additional_attributes or {})
                local_meta[project_key] = {
                    "project_id": extra.get("projectId") or project_key,
                    "display_name": item.display_name or "",
                }
                continue
            counter = _gcp_classify(asset_type, item.state or "", dict(item.labels or {}))
            if counter:
                local_counts[project_key][counter] += 1
        return asset_type, seen, local_counts, local_meta

    warn(f"Cloud Asset Inventory scan of {scope} ({len(asset_types)} asset types, read-only)...")
    with ThreadPoolExecutor(max_workers=min(args.workers, len(asset_types))) as pool:
        futures = {pool.submit(scan_type, t): t for t in asset_types}
        for future in as_completed(futures):
            asset_type = futures[future]
            try:
                _, seen, local_counts, local_meta = future.result()
            except core_exceptions.PermissionDenied as e:
                warn(f"  {asset_type}: PERMISSION DENIED -- results will be incomplete ({e.message})")
                continue
            except Exception as e:
                warn(f"  {asset_type}: FAILED -- results will be incomplete ({e})")
                continue
            warn(f"  {asset_type}: {seen} assets")
            with lock:
                project_meta.update(local_meta)
                for project_key, values in local_counts.items():
                    for counter, value in values.items():
                        counts[project_key][counter] += value

    # Projects with assets but no Project row (e.g. RM asset type unavailable) still report.
    for project_key in counts:
        project_meta.setdefault(project_key, {"project_id": project_key, "display_name": ""})
    return _gcp_results_from_counts(counts, project_meta)


def _gcp_scan_one_project(project_id, project_name, clients):
    """Original per-project collection, with the GKE double-count fixed."""
    from google.api_core import exceptions as core_exceptions
    from google.cloud import compute_v1, container_v1beta1, functions_v1, bigquery, bigtable, storage
    from googleapiclient.discovery import build

    c = _new_gcp_counts()

    # ------------------- Compute Instances (Running et Stopped uniquement) -------------------
    try:
        for _zone, resp in clients["compute"].aggregated_list(
                compute_v1.AggregatedListInstancesRequest(project=project_id)):
            for i in (resp.instances or []):
                if i.status not in ("RUNNING", "TERMINATED"):  # TERMINATED = stopped dans GCP
                    continue
                labels = dict(i.labels or {})
                key = "vm_with_containers" if "goog-gke-node" in labels else "vm_no_containers"
                c[key] += 1
    except core_exceptions.GoogleAPICallError as e:
        record_api_failure("Compute Engine", project_id, e)

    # ------------------- GKE clusters (informational: nodes come from the VM labels) ---------
    try:
        clusters = clients["gke"].list_clusters(
            container_v1beta1.ListClustersRequest(project_id=project_id, zone="-")).clusters
        c["gke_clusters"] += len(clusters)
    except core_exceptions.GoogleAPICallError as e:
        record_api_failure("Kubernetes Engine", project_id, e)

    # ------------------- Functions -------------------
    try:
        c["serverless"] += sum(1 for _ in clients["functions"].list_functions(
            request={"parent": f"projects/{project_id}/locations/-"}))
    except core_exceptions.GoogleAPICallError as e:
        record_api_failure("Cloud Functions", project_id, e)

    # ------------------- CloudRun -------------------
    try:
        cloudrun = build("run", "v1", cache_discovery=False)
        services = cloudrun.projects().locations().services().list(
            parent=f"projects/{project_id}/locations/-").execute().get("items", [])
        for s in services:
            labels = (s.get("metadata", {}).get("labels") or {})
            if labels.get("goog-managed-by") == "cloudfunctions":
                continue  # gen2 function, already counted as serverless
            c["caas"] += 1
    except Exception as e:
        record_api_failure("Cloud Run", project_id, e)

    # ------------------- Buckets -------------------
    try:
        c["buckets"] += sum(1 for _ in storage.Client(project=project_id).list_buckets())
    except core_exceptions.GoogleAPICallError as e:
        record_api_failure("Cloud Storage", project_id, e)

    # ------------------- BigQuery -------------------
    try:
        c["paas_db"] += sum(1 for _ in bigquery.Client(project=project_id).list_datasets())
    except core_exceptions.GoogleAPICallError as e:
        record_api_failure("BigQuery", project_id, e)

    # ------------------- Bigtable -------------------
    try:
        instances_list, _ = bigtable.Client(project=project_id, admin=True).list_instances()
        c["paas_db"] += len(instances_list)
    except (core_exceptions.GoogleAPICallError, ValueError) as e:
        record_api_failure("Bigtable", project_id, e)

    # ------------------- Cloud SQL -------------------
    try:
        sqladmin = build("sqladmin", "v1beta4", cache_discovery=False)
        instances = sqladmin.instances().list(project=project_id).execute().get("items", [])
        c["paas_db"] += sum(1 for i in instances
                            if str(i.get("state", "")).upper() not in _SQL_INACTIVE_STATES)
    except Exception as e:
        record_api_failure("Cloud SQL", project_id, e)

    account_info = {"Name": project_name, "Id": project_id}
    tables(account_info, [
        ["Compute Instances (no containers)", c["vm_no_containers"]],
        ["GKE Nodes (with containers)", c["vm_with_containers"]],
        ["GKE Clusters", c["gke_clusters"]],
        ["Google Functions", c["serverless"]],
        ["Google CloudRun (CaaS)", c["caas"]],
        ["Cloud Storage Buckets", c["buckets"]],
        ["PaaS Databases (BQ/Bigtable/CloudSQL)", c["paas_db"]],
    ])
    return licensing_count("GCP", c["vm_no_containers"], c["vm_with_containers"], c["caas"],
                           c["serverless"], c["buckets"], c["paas_db"], None, account_info)


def _gcp_list_projects():
    from googleapiclient.discovery import build
    service = build('cloudresourcemanager', 'v1', cache_discovery=False)
    request = service.projects().list(pageSize=1000)
    projects = []
    while request:
        response = request.execute()
        for project in response.get("projects", []):
            if project.get("lifecycleState") != "ACTIVE":
                continue
            projects.append({"projectId": project["projectId"], "name": project.get("name", "")})
        request = service.projects().list_next(previous_request=request, previous_response=response)
    return projects


def _load_checkpoint():
    if not args.checkpoint:
        return {}
    try:
        with open(args.checkpoint, encoding="utf-8") as fh:
            return {row["account_id"]: row for row in (json.loads(l) for l in fh if l.strip())}
    except FileNotFoundError:
        return {}


def _gcp_scan_per_project():
    """Original mode: per-project API calls. Now parallel, resumable, creates nothing."""
    from google.cloud import compute_v1, container_v1beta1, functions_v1

    projects = _gcp_list_projects()
    done = _load_checkpoint()
    todo = [p for p in projects if p["projectId"] not in done]
    warn(f"{len(projects)} active projects ({len(done)} already in checkpoint, {len(todo)} to scan) "
         f"with {args.workers} workers")
    if len(todo) > 2000:
        warn("WARNING: per-project mode on a large estate is slow and hits per-project API "
             "enablement gaps. Prefer --gcp-scope organizations/<ID> (Cloud Asset Inventory).")

    # Shared clients: v1 rebuilt a gRPC channel per project inside the loop.
    clients = {
        "compute": compute_v1.InstancesClient(),
        "gke": container_v1beta1.ClusterManagerClient(),
        "functions": functions_v1.CloudFunctionsServiceClient(),
    }

    results = list(done.values())
    checkpoint_lock = threading.Lock()
    checkpoint_fh = open(args.checkpoint, "a", encoding="utf-8") if args.checkpoint else None

    def run(p):
        return _gcp_scan_one_project(p["projectId"], p["name"], clients)

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = {pool.submit(run, p): p for p in todo}
            for n, future in enumerate(as_completed(futures), 1):
                project = futures[future]
                try:
                    result = future.result()
                except Exception as e:
                    warn(f"Project {project['projectId']} failed: {e}")
                    continue
                results.append(result)
                if checkpoint_fh:
                    with checkpoint_lock:
                        checkpoint_fh.write(json.dumps(result) + "\n")
                        checkpoint_fh.flush()
                if n % 100 == 0:
                    warn(f"  {n}/{len(todo)} projects scanned")
    finally:
        if checkpoint_fh:
            checkpoint_fh.close()
    print_api_failure_summary(len(todo))
    return results


# --- Ephemeral BigQuery mode -------------------------------------------------
# The ONLY mode that writes anything to the customer tenant. It creates a dataset
# named cortex_sizing_tmp_<uuid> in --bq-project and deletes it in a finally block,
# on SIGINT/SIGTERM and at interpreter exit. The dataset also carries a 6h default
# table expiration so BigQuery reclaims it even if the process is SIGKILLed.
_BQ_STATE_FILE = ".cortex_sizing_bq_state.json"
_bq_pending = {"client": None, "dataset_id": None}


def _bq_cleanup():
    client, dataset_id = _bq_pending.get("client"), _bq_pending.get("dataset_id")
    if not client or not dataset_id:
        return
    _bq_pending["dataset_id"] = None
    try:
        client.delete_dataset(dataset_id, delete_contents=True, not_found_ok=True)
        warn(f"Deleted ephemeral dataset {dataset_id}")
    except Exception as e:
        warn(f"COULD NOT DELETE {dataset_id}: {e}\n"
             f"Run: bq rm -r -f {dataset_id}   (it also self-expires within 6h)")
        return
    try:
        import os
        os.remove(_BQ_STATE_FILE)
    except OSError:
        pass


def _bq_signal(signum, _frame):
    _bq_cleanup()
    sys.exit(128 + signum)


def _gcp_bq_cleanup_only():
    from google.cloud import bigquery
    try:
        with open(_BQ_STATE_FILE, encoding="utf-8") as fh:
            state = json.load(fh)
    except FileNotFoundError:
        warn("No leftover ephemeral dataset recorded.")
        return
    _bq_pending["client"] = bigquery.Client(project=state["project"])
    _bq_pending["dataset_id"] = state["dataset_id"]
    _bq_cleanup()


def _gcp_scan_bq_export(scope):
    import os
    import uuid
    from google.cloud import asset_v1, bigquery

    bq_project = args.bq_project
    client = bigquery.Client(project=bq_project)
    dataset_name = f"cortex_sizing_tmp_{uuid.uuid4().hex[:12]}"
    dataset_id = f"{bq_project}.{dataset_name}"

    dataset = bigquery.Dataset(dataset_id)
    dataset.location = args.bq_location
    dataset.default_table_expiration_ms = 6 * 60 * 60 * 1000  # self-destruct safety net
    dataset.description = "EPHEMERAL - Cortex Cloud sizing scan. Safe to delete."

    warn(f"Creating EPHEMERAL dataset {dataset_id} (auto-expires in 6h, deleted at end of run)")
    client.create_dataset(dataset)
    _bq_pending["client"], _bq_pending["dataset_id"] = client, dataset_id
    with open(_BQ_STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump({"project": bq_project, "dataset_id": dataset_id}, fh)
    atexit.register(_bq_cleanup)
    signal.signal(signal.SIGINT, _bq_signal)
    signal.signal(signal.SIGTERM, _bq_signal)

    try:
        asset_client = asset_v1.AssetServiceClient()
        operation = asset_client.export_assets(request=asset_v1.ExportAssetsRequest(
            parent=scope,
            content_type=asset_v1.ContentType.RESOURCE,
            asset_types=GCP_ASSET_TYPES,
            output_config=asset_v1.OutputConfig(
                bigquery_destination=asset_v1.BigQueryDestination(
                    dataset=f"projects/{bq_project}/datasets/{dataset_name}",
                    table="assets", force=True, separate_tables_per_asset_type=False))))
        warn("Export running (this is a single org-wide operation)...")
        operation.result(timeout=3600)

        query = f"""
        SELECT
          SPLIT(ancestors[SAFE_OFFSET(0)], '/')[SAFE_OFFSET(1)] AS project_key,
          COUNTIF(asset_type = 'compute.googleapis.com/Instance'
                  AND JSON_VALUE(resource.data, '$.status') IN ('RUNNING','TERMINATED')
                  AND JSON_VALUE(resource.data, '$.labels."goog-gke-node"') IS NULL) AS vm_no_containers,
          COUNTIF(asset_type = 'compute.googleapis.com/Instance'
                  AND JSON_VALUE(resource.data, '$.status') IN ('RUNNING','TERMINATED')
                  AND JSON_VALUE(resource.data, '$.labels."goog-gke-node"') IS NOT NULL) AS vm_with_containers,
          COUNTIF(asset_type = 'run.googleapis.com/Service'
                  AND IFNULL(JSON_VALUE(resource.data, '$.metadata.labels."goog-managed-by"'), '')
                      != 'cloudfunctions') AS caas,
          COUNTIF(asset_type LIKE 'cloudfunctions.googleapis.com/%') AS serverless,
          COUNTIF(asset_type = 'storage.googleapis.com/Bucket') AS buckets,
          COUNTIF(asset_type IN ('bigquery.googleapis.com/Dataset',
                                 'bigtableadmin.googleapis.com/Instance')
                  OR (asset_type = 'sqladmin.googleapis.com/Instance'
                      AND IFNULL(UPPER(JSON_VALUE(resource.data, '$.state')), '')
                          NOT IN UNNEST({sorted(_SQL_INACTIVE_STATES)}))) AS paas_db,
          COUNTIF(asset_type = 'container.googleapis.com/Cluster') AS gke_clusters
        FROM `{bq_project}.{dataset_name}.assets`
        WHERE ARRAY_LENGTH(ancestors) > 0
        GROUP BY project_key
        """
        counts = defaultdict(_new_gcp_counts)
        for row in client.query(query).result():
            for key in ("vm_no_containers", "vm_with_containers", "caas", "serverless",
                        "buckets", "paas_db", "gke_clusters"):
                counts[row["project_key"]][key] = row[key]
        return _gcp_results_from_counts(counts, {})
    finally:
        _bq_cleanup()


def resolve_gcp_mode():
    """(mode, scope) from the flags. Shared by the preflight and the scan."""
    scope = _normalize_scope(args.gcp_scope)
    mode = args.gcp_mode
    if mode == "auto":
        mode = "asset" if scope and scope.split("/")[0] in ("organizations", "folders") else "project"
    # Validate before importing any SDK so the message is about the flag, not a missing package.
    if mode in ("asset", "bq") and not scope:
        raise SystemExit(f"--gcp-mode {mode} requires --gcp-scope organizations/<ID> (or folders/<ID>)")
    if mode == "bq" and not args.bq_project:
        raise SystemExit("--gcp-mode bq requires --bq-project (host of the EPHEMERAL dataset)")
    return mode, scope


def pcs_sizing_gcp():
    import google.auth

    if args.bq_cleanup_only:
        _gcp_bq_cleanup_only()
        return

    mode, scope = resolve_gcp_mode()
    warn(f"\n{separator}\nGetting Resources from GCP (mode={mode}"
         f"{', scope=' + scope if scope else ''})\n{separator}")

    try:
        if mode == "asset":
            results = _gcp_scan_asset_inventory(scope)
        elif mode == "bq":
            results = _gcp_scan_bq_export(scope)
        else:
            results = _gcp_scan_per_project()
    except google.auth.exceptions.DefaultCredentialsError as e:
        warn(f"GCP Authentication Error: {e}")
        return

    emit(results)


# ---------------------------- OCI ----------------------------
def pcs_sizing_oci():
    import oci
    results = []

    warn(f"\n{separator}\nGetting Resources from OCI\n{separator}")
    config = oci.config.from_file()
    identity = oci.identity.IdentityClient(config)
    compute = oci.core.ComputeClient(config)

    # v1 only listed direct children of the tenancy and only the first page.
    compartments = oci.pagination.list_call_get_all_results(
        identity.list_compartments, config['tenancy'],
        compartment_id_in_subtree=True, access_level="ACCESSIBLE",
        lifecycle_state="ACTIVE").data
    compartments_list = ([{"Name": "root", "Id": config['tenancy']}] +
                         [{"Name": c.name, "Id": c.id} for c in compartments])

    def scan(comp):
        # Ne compter que les instances en état RUNNING ou STOPPED
        instances = oci.pagination.list_call_get_all_results(
            compute.list_instances, compartment_id=comp['Id']).data
        compute_count = sum(1 for i in instances if i.lifecycle_state in ["RUNNING", "STOPPED"])

        # OCI: distinguishing VMs with/without containers would require more in-depth analysis
        account_info = {"Name": comp['Name'], "Id": comp['Id']}
        tables(account_info, [["Compute Instances (running/stopped)", compute_count]])
        return licensing_count("OCI", compute_count, 0, 0, 0, 0, 0, None, account_info)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(scan, c): c for c in compartments_list}
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except Exception as e:
                warn(f"OCI error on {futures[future]['Name']}: {e}")

    emit(results)


# ======================= BOOTSTRAP & PREFLIGHT =======================
# Everything below runs before the scan so that the operator has nothing to
# install, configure or look up by hand. All output goes to stderr.

# Only what the selected provider (and GCP mode) actually needs gets installed.
DEPENDENCIES = {
    "aws": ["boto3", "botocore"],
    "azure": ["azure-identity", "azure-mgmt-compute", "azure-mgmt-containerservice",
              "azure-mgmt-subscription", "azure-mgmt-web", "azure-mgmt-sql",
              "azure-mgmt-cosmosdb", "azure-mgmt-storage", "azure-mgmt-containerregistry"],
    "oci": ["oci"],
    "gcp:asset": ["google-cloud-asset"],
    "gcp:bq": ["google-cloud-asset", "google-cloud-bigquery"],
    "gcp:project": ["google-cloud-compute", "google-cloud-container", "google-cloud-functions",
                    "google-cloud-bigquery", "google-cloud-bigtable", "google-cloud-storage",
                    "google-api-python-client", "google-auth"],
}
IMPORT_PROBES = {
    "aws": ["boto3"],
    "azure": ["azure.identity", "azure.mgmt.compute", "azure.mgmt.containerservice",
              "azure.mgmt.subscription", "azure.mgmt.web", "azure.mgmt.sql",
              "azure.mgmt.cosmosdb", "azure.mgmt.storage", "azure.mgmt.containerregistry"],
    "oci": ["oci"],
    "gcp:asset": ["google.cloud.asset_v1"],
    "gcp:bq": ["google.cloud.asset_v1", "google.cloud.bigquery"],
    "gcp:project": ["google.cloud.compute_v1", "google.cloud.container_v1beta1",
                    "google.cloud.functions_v1", "google.cloud.bigquery",
                    "google.cloud.bigtable", "google.cloud.storage", "googleapiclient.discovery"],
}

_BOOTSTRAP_MARKER = "CORTEX_SIZING_BOOTSTRAPPED"
_HANDOVER_MARKER = "CORTEX_SIZING_HANDED_OVER"
_COLOR = sys.stderr.isatty()


def _c(code, text):
    return f"\033[{code}m{text}\033[0m" if _COLOR else text


def step(msg):
    warn(_c("1", f"\n==> {msg}"))


def info(msg):
    warn(f"    {msg}")


def ok(msg):
    warn(f"    {_c('32', 'OK')}  {msg}")


def fail(msg):
    warn(f"\n{_c('31', 'ERROR')} {msg}\n")
    sys.exit(1)


def ask(question, default_yes=False):
    """Prompt, unless --yes or there is no terminal to prompt on."""
    if args.yes:
        info(f"{question} -> yes (--yes)")
        return True
    if not sys.stdin.isatty():
        info(f"{question} -> no (not interactive; re-run with --yes to accept)")
        return False
    suffix = "[Y/n]" if default_yes else "[y/N]"
    try:
        sys.stderr.write(f"    {question} {suffix} ")
        sys.stderr.flush()
        reply = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        warn("")
        return False
    if not reply:
        return default_yes
    return reply.startswith("y")


def run_cmd(cmd, capture=True):
    """Run an external command. Returns (returncode, stdout, stderr)."""
    import subprocess
    try:
        proc = subprocess.run(cmd, capture_output=capture, text=True, check=False)
    except (OSError, ValueError) as e:
        return 127, "", str(e)
    return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()


def has_cmd(name):
    import shutil
    return shutil.which(name) is not None


def detect_environment():
    import platform
    if os.environ.get("CLOUD_SHELL") or os.environ.get("GOOGLE_CLOUD_SHELL"):
        return "Google Cloud Shell"
    if os.environ.get("AZUREPS_HOST_ENVIRONMENT", "").startswith("cloud-shell"):
        return "Azure Cloud Shell"
    if os.environ.get("AWS_EXECUTION_ENV", "").startswith("CloudShell"):
        return "AWS CloudShell"
    if os.environ.get("CI"):
        return f"CI ({platform.system()})"
    return f"local machine ({platform.system()})"


def dependency_key(cloud):
    if cloud != "gcp":
        return cloud
    if args.bq_cleanup_only:
        return "gcp:bq"
    mode, _ = resolve_gcp_mode()
    return f"gcp:{mode}"


def missing_imports(key):
    """
    Probe by ACTUALLY importing each module.

    find_spec() only proves a file is on disk; it never executes it. A package
    can be present and still explode on import -- typically a stale pyOpenSSL in
    ~/.local shadowing the system one and no longer matching its cryptography
    ("module 'lib' has no attribute 'GEN_EMAIL'"), which happens on shared
    images such as Cloud Shell. Catching that here costs a couple of seconds and
    saves a traceback thirty seconds into the scan.

    Returns (missing, broken):
      missing  not installed          -> install it
      broken   installed but unusable -> needs a clean environment
    """
    import importlib
    missing, broken = [], []
    for module in IMPORT_PROBES[key]:
        try:
            importlib.import_module(module)
        except ImportError:                      # includes ModuleNotFoundError
            missing.append(module)
        except Exception as e:                   # version conflict, broken C ext, ...
            broken.append(f"{module}: {type(e).__name__}: {e}")
    return missing, broken


def venv_python(venv_dir):
    if os.name == "nt":
        return os.path.join(venv_dir, "Scripts", "python.exe")
    return os.path.join(venv_dir, "bin", "python")


def ensure_dependencies(cloud):
    """
    Make the SDKs for `cloud` importable, installing them if needed.

    Order of preference:
      1. already importable            -> nothing to do
      2. running inside a virtualenv   -> install into it
      3. otherwise                     -> create ./venv, install, re-exec in it
    A marker env var makes the re-exec non-recursive.
    """
    import subprocess

    key = dependency_key(cloud)
    step("Dependencies")
    missing, broken = missing_imports(key)
    if not missing and not broken:
        ok(f"{len(IMPORT_PROBES[key])} module(s) available for {key}")
        return

    if missing:
        info(f"missing for {key}: {', '.join(missing)}")
    if broken:
        info(f"installed but unusable for {key}:")
        for detail in broken:
            info(f"  {detail}")
        info("a package in this environment conflicts with another (often a stale")
        info("~/.local install shadowing the system one)")
    packages = DEPENDENCIES[key]

    if args.no_bootstrap:
        fail(("Dependencies are broken" if broken else "Dependencies are missing") +
             " and --no-bootstrap was given. Fix with:\n"
             f"     python3 -m pip install --upgrade {' '.join(packages)}")

    in_venv = sys.prefix != sys.base_prefix
    already_tried = os.environ.get(_BOOTSTRAP_MARKER) == "1"

    if in_venv:
        target_python = sys.executable
        info(f"installing into the active virtualenv ({sys.prefix})")
    else:
        # Installing next to a broken copy would not help: ~/.local keeps priority.
        # A venv ignores user site-packages entirely (PEP 405), so it is the only
        # reliable escape from a poisoned ambient environment.
        if broken:
            info("a virtualenv ignores ~/.local, which is what makes this recoverable")
        venv_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "venv")
        target_python = venv_python(venv_dir)
        handed_over = os.environ.get(_HANDOVER_MARKER) == "1"
        if os.path.exists(target_python) and not handed_over:
            # The venv is already there from a previous run. Hand over immediately
            # rather than reinstalling: the child re-probes in the clean
            # environment and installs only what it actually lacks. Without this,
            # every invocation paid ~30s of pip for nothing.
            # The marker stops a second handover if that python is not a real venv.
            info(f"reusing {venv_dir}")
            info("restarting in the prepared environment ...")
            env = dict(os.environ, **{_HANDOVER_MARKER: "1"})
            result = subprocess.run([target_python, os.path.abspath(__file__), *sys.argv[1:]],
                                    env=env)
            sys.exit(result.returncode)
        question = (f"Create a clean virtualenv in {venv_dir}?" if broken else
                    f"Create a virtualenv in {venv_dir} and install {len(packages)} package(s)?")
        if not ask(question, default_yes=True):
            fail("Cannot continue in this environment. Either repair it:\n"
                 f"     python3 -m pip install --user --upgrade {' '.join(packages)}\n"
                 "     or create a virtualenv yourself and run the script from it.")
        info(f"creating {venv_dir} ...")
        try:
            subprocess.run([sys.executable, "-m", "venv", venv_dir], check=True)
        except (subprocess.CalledProcessError, OSError) as e:
            fail(f"could not create the virtualenv: {e}\n"
                 "     On Debian/Ubuntu you may need:  sudo apt install python3-venv")

    if already_tried:
        fail("Dependencies are still not usable after a bootstrap attempt.\n"
             f"     Try manually: {target_python} -m pip install --upgrade {' '.join(packages)}\n"
             "     If a module is 'installed but unusable', delete ./venv and retry.")

    info(f"installing {', '.join(packages)} (this takes ~30s) ...")
    code, _out, err = run_cmd([target_python, "-m", "pip", "install", "--quiet",
                               "--disable-pip-version-check", *packages])
    if code != 0:
        fail(f"dependency install failed:\n{err}\n"
             "     Check network access / proxy settings, then retry.")
    ok("dependencies installed")

    # Re-exec so the new packages are importable. subprocess (not execv) keeps the
    # behaviour identical on Windows.
    env = dict(os.environ, **{_BOOTSTRAP_MARKER: "1"})
    info("restarting in the prepared environment ...")
    result = subprocess.run([target_python, os.path.abspath(__file__), *sys.argv[1:]], env=env)
    sys.exit(result.returncode)


# ---- authentication -------------------------------------------------------
def check_auth_gcp():
    import google.auth
    from google.auth import exceptions as auth_exceptions

    step("Authentication")
    if has_cmd("gcloud"):
        code, account, _ = run_cmd(["gcloud", "config", "get-value", "account"])
        if code == 0 and account and account != "(unset)":
            info(f"gcloud account: {account}")
    try:
        _creds, project = google.auth.default()
    except auth_exceptions.DefaultCredentialsError:
        fail("No Application Default Credentials. Run:\n"
             "     gcloud auth application-default login")
    quota_project = os.environ.get("GOOGLE_CLOUD_QUOTA_PROJECT") or project
    if not quota_project and has_cmd("gcloud"):
        code, configured, _ = run_cmd(["gcloud", "config", "get-value", "project"])
        if code == 0 and configured and configured != "(unset)":
            quota_project = configured
            os.environ["GOOGLE_CLOUD_QUOTA_PROJECT"] = quota_project
    if not quota_project:
        fail("No quota project. Pick any project you can use for API billing:\n"
             "     gcloud config set project MY-PROJECT")
    ok(f"credentials valid, API calls billed to {quota_project}")
    ok("the scanned projects are never charged and never modified")
    return quota_project


def check_auth_aws():
    import boto3
    import botocore
    step("Authentication")
    try:
        identity = boto3.client("sts", config=_boto_config()).get_caller_identity()
    except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as e:
        fail("AWS credentials are not usable:\n"
             f"     {e}\n"
             "     Fix with one of:  aws configure  |  aws sso login  |  export AWS_PROFILE=...")
    ok(f"account {identity['Account']} as {identity['Arn'].rsplit('/', 1)[-1]}")
    return identity["Account"]


def check_auth_azure():
    from azure.identity import DefaultAzureCredential
    step("Authentication")
    try:
        DefaultAzureCredential().get_token("https://management.azure.com/.default")
    except Exception as e:
        fail("Azure credentials are not usable:\n"
             f"     {e}\n"
             "     Fix with:  az login")
    ok("credentials valid")
    return None


def check_auth_oci():
    import oci
    step("Authentication")
    try:
        config = oci.config.from_file()
        oci.config.validate_config(config)
    except Exception as e:
        fail("OCI configuration is not usable:\n"
             f"     {e}\n"
             "     Fix with:  oci setup config")
    ok(f"tenancy {config['tenancy']}")
    return config["tenancy"]


# ---- GCP scope and API ----------------------------------------------------
def gcp_autodetect_scope():
    """
    Decide what to scan when the operator gave no --gcp-scope.

    A whole-organisation scan is dramatically faster and needs fewer permissions,
    so it is offered by default whenever exactly one organisation is visible.
    """
    if args.gcp_scope or args.gcp_mode != "auto":
        return
    if not has_cmd("gcloud"):
        info("gcloud not found: cannot auto-detect the organisation, staying in per-project mode")
        info("for an organisation-wide scan pass:  --gcp-scope organizations/<ID>")
        return
    code, out, _ = run_cmd(["gcloud", "organizations", "list", "--format=value(ID)"])
    orgs = [line for line in out.splitlines() if line.strip()] if code == 0 else []
    if not orgs:
        info("no organisation visible: staying in per-project mode")
        return
    if len(orgs) > 1:
        info(f"several organisations visible: {', '.join(orgs)}")
        info("pick one with:  --gcp-scope organizations/<ID>")
        return
    org = orgs[0]
    info(f"organisation {org} detected")
    info("an organisation-wide scan is far faster and read-only")
    if ask(f"Scan the whole organisation {org}?", default_yes=True):
        args.gcp_scope = f"organizations/{org}"
    else:
        info("staying in per-project mode")


def gcp_check_access(scope, quota_project):
    """
    One real Cloud Asset call. It validates the API enablement, the IAM role and
    the scope at once, and turns each failure into an actionable instruction.
    """
    from google.cloud import asset_v1
    from google.api_core import exceptions as core_exceptions

    step("Cloud Asset Inventory access")
    request = asset_v1.SearchAllResourcesRequest(
        scope=scope, asset_types=["cloudresourcemanager.googleapis.com/Project"], page_size=1)
    try:
        next(iter(asset_v1.AssetServiceClient().search_all_resources(request=request)), None)
    except core_exceptions.PermissionDenied as e:
        message = str(e)
        if "has not been used in project" in message or "it is disabled" in message:
            gcp_offer_enable_api(quota_project)
            return gcp_check_access(scope, quota_project)  # retry once it is on
        fail(f"read access to {scope} was refused:\n"
             f"     {e.message}\n\n"
             "     Ask an Organisation Admin to run:\n"
             f"     gcloud organizations add-iam-policy-binding {scope.split('/')[-1]} \\\n"
             "         --member=user:YOUR_EMAIL --role=roles/cloudasset.viewer")
    except core_exceptions.InvalidArgument as e:
        fail(f"{scope} is not a valid scope: {e.message}")
    except core_exceptions.GoogleAPICallError as e:
        fail(f"Cloud Asset Inventory call failed: {e}")
    ok(f"read access to {scope} confirmed")


def gcp_offer_enable_api(quota_project):
    command = ["gcloud", "services", "enable", "cloudasset.googleapis.com",
               "--project", quota_project]
    warn("")
    info(f"cloudasset.googleapis.com is NOT enabled on {quota_project}.")
    info("It is needed on this ONE project only, never on the scanned projects.")
    info("It is a project setting, not a deployed resource: free and reversible with")
    info(f"  gcloud services disable cloudasset.googleapis.com --project {quota_project}")
    warn("")
    info(_c("1", " ".join(command)))
    warn("")
    if not has_cmd("gcloud"):
        fail("gcloud is not installed, so the command above cannot be run for you.\n"
             "     Enable the API from the console instead:\n"
             "     https://console.cloud.google.com/apis/library/cloudasset.googleapis.com"
             f"?project={quota_project}")
    if not ask("Run this command now?", default_yes=False):
        fail("Cloud Asset Inventory is required for an organisation scan.\n"
             "     Run the command above, then start this script again.")
    info("enabling ...")
    code, _out, err = run_cmd(command)
    if code != 0:
        fail(f"could not enable the API:\n{err}\n"
             f"     You need roles/serviceusage.serviceUsageAdmin on {quota_project}.\n"
             "     Any project you control works as the quota project; switch with:\n"
             "       gcloud config set project MY-PROJECT\n"
             "     or   export GOOGLE_CLOUD_QUOTA_PROJECT=MY-PROJECT")
    ok("enabled (allow up to a minute to propagate)")
    import time
    time.sleep(10)


def preflight(cloud):
    step(f"Cortex Cloud sizing - {cloud.upper()}")
    info(f"environment: {detect_environment()}")
    info("this scan is read-only; it creates nothing in the tenant"
         + (" (except --gcp-mode bq, see --help)" if cloud == "gcp" else ""))

    if cloud == "gcp":
        gcp_autodetect_scope()

    ensure_dependencies(cloud)

    if cloud == "gcp":
        quota_project = check_auth_gcp()
        mode, scope = resolve_gcp_mode()
        step("Scan plan")
        info(f"mode {mode}" + (f", scope {scope}" if scope else ""))
        if mode == "asset":
            info("one organisation-wide Cloud Asset Inventory read; minutes, not hours")
        elif mode == "project":
            info("per-project API calls; prefer --gcp-scope organizations/<ID> beyond ~50 projects")
        elif mode == "bq":
            info(f"creates an EPHEMERAL BigQuery dataset in {args.bq_project}, deleted at the end")
        if mode in ("asset", "bq"):
            gcp_check_access(scope, quota_project)
    elif cloud == "aws":
        check_auth_aws()
    elif cloud == "azure":
        check_auth_azure()
    elif cloud == "oci":
        check_auth_oci()

    if args.check:
        step("All preflight checks passed")
        info("re-run without --check to start the scan")
        sys.exit(0)


# ---------------------------- MAIN ----------------------------
SCANNERS = {
    "aws": pcs_sizing_aws,
    "azure": pcs_sizing_az,
    "gcp": pcs_sizing_gcp,
    "oci": pcs_sizing_oci,
}

if __name__ == '__main__':
    selected = [name for name in ("aws", "azure", "gcp", "oci") if getattr(args, name)]
    if not selected:
        cortex_cloud_metering()
        print("\nYou must specify a cloud provider:\n--aws | --azure | --gcp | --oci")
        sys.exit(2)
    if len(selected) > 1:
        raise SystemExit(f"Pick a single cloud provider, got: {', '.join('--' + s for s in selected)}")

    cloud = selected[0]
    try:
        preflight(cloud)
    except KeyboardInterrupt:
        warn("\nInterrupted.")
        sys.exit(130)
    cortex_cloud_metering()
    SCANNERS[cloud]()
