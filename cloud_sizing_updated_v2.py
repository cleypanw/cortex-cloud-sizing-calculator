import json, os, argparse, math
import boto3, botocore
from botocore.exceptions import ClientError

parser = argparse.ArgumentParser()
parser.add_argument("--azure", "-az", help="Sizing for Azure", action='store_true')
parser.add_argument("--aws", "-a", help="Sizing for AWS", action='store_true')
parser.add_argument("--gcp", "-g", help="Sizing for GCP", action='store_true')
parser.add_argument("--oci", "-o", help="Sizing for OCI", action='store_true')
parser.add_argument("--region-prefix", "-rp", help="Filter AWS regions by prefix (e.g. us, eu, ap)", default=None)
parser.add_argument("--output", "-out", help="Output format (table/json)", default="table", choices=["table", "json"])
args = parser.parse_args()
separator = "-"*100

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
    print(f"\n{separator}\nCortex Cloud Workload Metering\n{separator}")
    print(f"{'Workload Type':<45} {'Billable Units':<50}\n{separator}")
    for workload, units in cc_metering_table:
        print(f"{workload:<45} {units:<50}")
    print(separator)

def tables(account_info, data):
    if args.output == "json":
        return
    print(f"{'Account':<50} {'Service':<40} {'Count':<10}\n{separator}")
    account = f'{account_info["Id"]} ({account_info["Name"]})' if account_info else ""
    for a, b in data:
        print(f"{account:<50} {a:<40} {b:<10}")
    print(separator)

def licensing_count(cloud, vm_no_containers, vm_with_containers, caas, serverless, buckets, paas_db, container_images=0, account_info=None):
    """
    Calculate the number of workloads (SKU) required based on new metrics
    
    Note: Container images have a free quota of 10 scans per deployed workload (VM/CaaS)
    Calculation of images beyond the free quota is not implemented here
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
    
    if args.output == "table":
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
        print(f"\nContainer Images: {container_images} (Free quota: {free_image_scans} scans)")
        
        print(f"\n{'='*60}")
        print(f"TOTAL: {total} Cortex Cloud workload(s) (SKU) needed for {cloud}")
        print(f"{'='*60}\n{separator}")
    
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

def print_global_summary(results):
    """
    Display a global summary table with the total SKUs per account/subscription/project
    """
    if not results:
        return
    
    print(f"\n\n{'='*120}")
    print(f"{'GLOBAL SKU SUMMARY - ALL ACCOUNTS/SUBSCRIPTIONS/PROJECTS':^120}")
    print(f"{'='*120}")
    print(f"{'Cloud':<15} {'Account/Subscription/Project ID':<45} {'Account Name':<35} {'SKU':<10}")
    print(f"{'-'*120}")
    
    total_sku = 0
    cloud_totals = {}
    
    for result in results:
        cloud = result['cloud']
        account_id = result['account_id']
        account_name = result['account_name']
        sku = result['total_workloads']
        
        # Truncate names that are too long
        account_name_display = (account_name[:32] + '...') if len(account_name) > 35 else account_name
        
        print(f"{cloud:<15} {account_id:<45} {account_name_display:<35} {sku:<10}")
        
        total_sku += sku
        cloud_totals[cloud] = cloud_totals.get(cloud, 0) + sku
    
    print(f"{'-'*120}")
    
    # Subtotals by cloud
    if len(cloud_totals) > 1:
        print(f"\n{'SUBTOTALS BY CLOUD PROVIDER':^120}")
        print(f"{'-'*120}")
        for cloud, subtotal in cloud_totals.items():
            print(f"{cloud:<15} {'Subtotal':<80} {subtotal:<10}")
        print(f"{'-'*120}")
    
    # Grand total
    print(f"\n{'GRAND TOTAL':<95} {total_sku:<10}")
    print(f"{'='*120}\n")
    
    return total_sku

# ---------------------------- AWS ----------------------------
def aws(account, session=None):
    if session is None:
        session = boto3.Session()

    try:
        regions = [r['RegionName'] for r in session.client('ec2').describe_regions()['Regions']]
        if args.region_prefix:
            regions = [r for r in regions if r.startswith(args.region_prefix)]
    except botocore.exceptions.ClientError as error:
        raise error

    ec2_all = eks_all = ecs_all = fargate_all = lambdas_all = rds_all = dynamodb_all = efs_all = ecr_images = 0

    # ---------------- S3 Buckets (global) ----------------
    try:
        s3 = session.client('s3')
        s3_all = len(s3.list_buckets()['Buckets'])
    except botocore.exceptions.ClientError as error:
        s3_all = 0
        print(f"S3 error: {error}")

    # ---------------- ECR Images (global) ----------------
    try:
        ecr = session.client('ecr')
        repositories = ecr.describe_repositories()['repositories']
        for repo in repositories:
            try:
                images = ecr.list_images(repositoryName=repo['repositoryName'])['imageIds']
                ecr_images += len(images)
            except:
                continue
    except botocore.exceptions.ClientError as error:
        ecr_images = 0
        print(f"ECR error: {error}")

    # ---------------- Regional Services ----------------
    for region in regions:
        try:
            ec2 = session.client('ec2', region_name=region)
            ec2_group = ec2.describe_instances(
                Filters=[{'Name': 'instance-state-code', 'Values': ["16"]}]
            )['Reservations']
            
            # Count EC2 and distinguish EKS nodes
            for ec2_item in ec2_group:
                for instance in ec2_item['Instances']:
                    tags = instance.get('Tags', [])
                    if any("eks:" in tag.get("Key", "") for tag in tags):
                        eks_all += 1
                    else:
                        ec2_all += 1
        except botocore.exceptions.ClientError as error:
            print(f"EC2 error in {region}: {error}")

        # ECS Tasks (managed containers)
        try:
            ecs_client = session.client('ecs', region_name=region)
            clusters = ecs_client.list_clusters()['clusterArns']
            for cluster in clusters:
                tasks = ecs_client.list_tasks(cluster=cluster, desiredStatus='RUNNING')['taskArns']
                ecs_all += len(tasks)
            
            # Fargate task definitions
            fargate_all += len(ecs_client.list_task_definitions()['taskDefinitionArns'])
        except botocore.exceptions.ClientError as error:
            print(f"ECS error in {region}: {error}")

        try:
            lambda_client = session.client('lambda', region_name=region)
            lambdas_all += len(lambda_client.list_functions()['Functions'])
        except botocore.exceptions.ClientError as error:
            print(f"Lambda error in {region}: {error}")

        try:
            rds = session.client('rds', region_name=region)
            rds_all += len(rds.describe_db_instances()['DBInstances'])
        except botocore.exceptions.ClientError as error:
            print(f"RDS error in {region}: {error}")

        try:
            dynamodb = session.client('dynamodb', region_name=region)
            dynamodb_all += len(dynamodb.list_tables()['TableNames'])
        except botocore.exceptions.ClientError as error:
            print(f"DynamoDB error in {region}: {error}")

        try:
            efs = session.client('efs', region_name=region)
            efs_all += len(efs.describe_file_systems()['FileSystems'])
        except botocore.exceptions.ClientError as error:
            print(f"EFS error in {region}: {error}")

    # Calculation: EC2 without containers vs EKS (with containers)
    vm_no_containers = ec2_all
    vm_with_containers = eks_all
    caas = ecs_all  # ECS running tasks
    serverless = lambdas_all
    buckets = s3_all
    paas_db = rds_all + dynamodb_all + efs_all

    tables(account, [
        ["EC2 Instances (no containers)", vm_no_containers],
        ["EKS Nodes (with containers)", vm_with_containers],
        ["ECS Tasks (CaaS)", caas],
        ["Fargate Task Definitions", fargate_all],
        ["Lambda Functions", lambdas_all],
        ["S3 Buckets", s3_all],
        ["RDS Instances", rds_all],
        ["DynamoDB Tables", dynamodb_all],
        ["EFS Systems", efs_all],
        ["ECR Container Images", ecr_images]
    ])
    
    return licensing_count("AWS", vm_no_containers, vm_with_containers, caas, serverless, buckets, paas_db, ecr_images, account)

def pcs_sizing_aws():
    sts = boto3.client("sts")
    iam = boto3.client('iam')
    org = boto3.client('organizations')
    accounts = []
    results = []

    aliases = iam.list_account_aliases().get('AccountAliases', [])
    account_info = {
        "Name": aliases[0] if aliases else 'No alias',
        "Id": sts.get_caller_identity()["Account"]
    }
    result = aws(account_info)
    results.append(result)

    try:
        paginator = org.get_paginator('list_accounts')
        for page in paginator.paginate():
            for acct in page['Accounts']:
                if acct['Status'] == "ACTIVE":
                    accounts.append(acct)
    except botocore.exceptions.ClientError as error:
        print(f"{error}\n{separator}")

    for account in accounts:
        role_arn = f"arn:aws:iam::{account['Id']}:role/OrganizationAccountAccessRole"
        try:
            creds = boto3.client('sts').assume_role(
                RoleArn=role_arn, RoleSessionName='CrossAccountSession'
            )['Credentials']
            session = boto3.Session(
                aws_access_key_id=creds['AccessKeyId'],
                aws_secret_access_key=creds['SecretAccessKey'],
                aws_session_token=creds['SessionToken']
            )
            result = aws({"Name": account['Name'], "Id": account['Id']}, session=session)
            results.append(result)
        except botocore.exceptions.ClientError as error:
            print(f"Error with {account['Name']} - {account['Id']}:\n{error}\n{separator}")
            continue

    if args.output == "json":
        print(json.dumps(results, indent=2))
    else:
        print_global_summary(results)

# ---------------------------- Azure ----------------------------
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

    print(f"\n{separator}\nGetting Resources from AZURE\n{separator}")

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

        # ------------------- VMs -------------------
        vm_list = []
        for vm in compute_client.virtual_machines.list_all():
            try:
                instance_view = compute_client.virtual_machines.instance_view(
                    vm.id.split('/')[4], vm.name
                )
                # Fix: check all statuses
                if any('PowerState/running' in s.code for s in instance_view.statuses):
                    vm_list.append(vm.name)
            except Exception as e:
                print(f"VM error for {vm.name}: {e}")
                continue

        # ------------------- AKS Nodes -------------------
        node_count = 0
        for cl in containerservice_client.managed_clusters.list():
            try:
                for ap in containerservice_client.agent_pools.list(
                    cl.id.split('/')[4], cl.name
                ):
                    node_count += ap.count or 0
            except Exception as e:
                print(f"AKS error: {e}")
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
                print(f"SQL error: {e}")
                continue

        # ------------------- Cosmos DB -------------------
        cosmos_count = sum(
            1 for acc in cosmos_client.database_accounts.list()
            if getattr(acc, "public_network_access", None) == "Enabled"
        )

        # ------------------- Storage Accounts -------------------
        storage_count = sum(1 for _ in storage_client.storage_accounts.list())

        # ------------------- Container Registry Images -------------------
        acr_images = 0
        try:
            for registry in acr_client.registries.list():
                # Note: counting images requires Docker Registry v2 API
                # For simplicity, we count registries * average estimate
                acr_images += 10  # Estimate
        except Exception as e:
            print(f"ACR error: {e}")

        # Metrics calculation
        vm_no_containers = len(vm_list)
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
            ["ACR Images (estimated)", acr_images]
        ])
        
        result = licensing_count(
            "Azure",
            vm_no_containers,
            vm_with_containers,
            caas,
            serverless,
            buckets,
            paas_db,
            acr_images,
            account_info
        )
        results.append(result)

    if args.output == "json":
        print(json.dumps(results, indent=2))
    else:
        print_global_summary(results)

# ---------------------------- GCP ----------------------------
def pcs_sizing_gcp():
    import google.auth
    from google.api_core import exceptions as core_exceptions
    from google.cloud import compute_v1, container_v1beta1, functions_v1, bigquery, bigtable, storage
    from googleapiclient.discovery import build
    
    results = []

    print(f"\n{separator}\nGetting Resources from GCP\n{separator}")
    
    try:
        service = build('cloudresourcemanager', 'v1')
    except google.auth.exceptions.DefaultCredentialsError as e:
        print(f"GCP Authentication Error: {e}")
        return
    
    request = service.projects().list()
    projects = []

    while request:
        response = request.execute()
        for project in response.get("projects", []):
            projects.append({
                "projectId": project["projectId"], 
                "name": project.get("name",""), 
                "lifecycleState": project.get("lifecycleState","")
            })
        request = service.projects().list_next(previous_request=request, previous_response=response)

    for p in projects:
        if p['lifecycleState'] != "ACTIVE":
            continue
        project_id = p['projectId']
        project_name = p['name']

        # Initialize counters
        compute_list = []
        node_count = 0
        gcp_functions = []
        gcp_cloudRun = []
        gcp_buckets = []
        gcp_bigquery_ds = []
        gcp_bigtables = []
        gcp_cloudsql = []
        gcr_images = 0

        # ------------------- Compute Instances -------------------
        try:
            compute_list = [
                i.name for zone, resp in compute_v1.InstancesClient().aggregated_list(
                    compute_v1.AggregatedListInstancesRequest(project=project_id)
                ) if resp.instances for i in resp.instances if i.status=="RUNNING"
            ]
        except core_exceptions.GoogleAPICallError as e:
            print(f"Compute Engine API error in {project_id}: {e}")

        # ------------------- GKE Nodes -------------------
        try:
            gke_client = container_v1beta1.ClusterManagerClient()
            node_count = sum(c.current_node_count for c in gke_client.list_clusters(
                container_v1beta1.ListClustersRequest(project_id=project_id, zone="-")
            ).clusters)
        except core_exceptions.GoogleAPICallError as e:
            print(f"Container API error in {project_id}: {e}")

        # ------------------- Functions -------------------
        try:
            gcp_functions = [
                fn.name for fn in functions_v1.CloudFunctionsServiceClient().list_functions(
                    request={"parent": f"projects/{project_id}/locations/-"}
                )
            ]
        except core_exceptions.GoogleAPICallError as e:
            print(f"Cloud Functions API error in {project_id}: {e}")

        # ------------------- CloudRun -------------------
        try:
            cloudrun = build("run", "v1")
            gcp_cloudRun = [
                s["metadata"]["name"] for s in cloudrun.projects().locations().services().list(
                    parent=f"projects/{project_id}/locations/-"
                ).execute().get("items", [])
            ]
        except Exception as e:
            print(f"Cloud Run API error in {project_id}: {e}")

        # ------------------- Buckets -------------------
        try:
            gcp_buckets = [b.name for b in storage.Client(project=project_id).list_buckets()]
        except core_exceptions.GoogleAPICallError as e:
            print(f"Cloud Storage API error in {project_id}: {e}")
        
        # ------------------- BigQuery -------------------
        try:
            gcp_bigquery_ds = [ds.dataset_id for ds in bigquery.Client(project=project_id).list_datasets()]
        except core_exceptions.GoogleAPICallError as e:
            print(f"BigQuery API error in {project_id}: {e}")
        
        # ------------------- Bigtable -------------------
        try:
            client = bigtable.Client(project=project_id, admin=True)
            instances_list, _ = client.list_instances() 
            gcp_bigtables = [i.instance_id for i in instances_list]
        except (core_exceptions.GoogleAPICallError, ValueError) as e:
            print(f"Bigtable API error in {project_id}: {e}")

        # ------------------- Cloud SQL -------------------
        try:
            sqladmin = build("sqladmin", "v1beta4")
            gcp_cloudsql = [
                i["name"] for i in sqladmin.instances().list(project=project_id).execute().get("items", []) 
                if i.get("state")=="RUNNABLE"
            ]
        except Exception as e:
            print(f"Cloud SQL API error in {project_id}: {e}")

        # ------------------- GCR/Artifact Registry Images -------------------
        try:
            # Estimate - would require Artifact Registry API for accurate count
            gcr_images = len(gcp_buckets) * 5  # Estimate
        except Exception as e:
            print(f"GCR error in {project_id}: {e}")

        # Metrics calculation
        vm_no_containers = len(compute_list)
        vm_with_containers = node_count
        caas = len(gcp_cloudRun)
        serverless = len(gcp_functions)
        buckets = len(gcp_buckets)
        paas_db = len(gcp_bigquery_ds) + len(gcp_bigtables) + len(gcp_cloudsql)

        account_info = {"Name": project_name, "Id": project_id}
        tables(account_info, [
            ["Compute Instances (no containers)", vm_no_containers],
            ["GKE Nodes (with containers)", vm_with_containers],
            ["Google Functions", len(gcp_functions)],
            ["Google CloudRun (CaaS)", caas],
            ["Cloud Storage Buckets", buckets],
            ["BigQuery Datasets", len(gcp_bigquery_ds)],
            ["BigTable instances", len(gcp_bigtables)],
            ["CloudSQL instances", len(gcp_cloudsql)],
            ["GCR Images (estimated)", gcr_images]
        ])
        
        result = licensing_count(
            "GCP", 
            vm_no_containers,
            vm_with_containers,
            caas,
            serverless,
            buckets,
            paas_db,
            gcr_images,
            account_info
        )
        results.append(result)

    if args.output == "json":
        print(json.dumps(results, indent=2))
    else:
        print_global_summary(results)

# ---------------------------- OCI ----------------------------
def pcs_sizing_oci():
    import oci
    results = []
    
    print(f"\n{separator}\nGetting Resources from OCI\n{separator}")
    config = oci.config.from_file()
    identity = oci.identity.IdentityClient(config)
    compute = oci.core.ComputeClient(config)

    compartments = identity.list_compartments(compartment_id=config['tenancy']).data
    compartments_list = [{"Name":"root","Id":config['tenancy']}] + [{"Name":c.name,"Id":c.id} for c in compartments]

    for comp in compartments_list:
        compute_count = sum(
            1 for i in compute.list_instances(compartment_id=comp['Id']).data 
            if i.lifecycle_state=="RUNNING"
        )
        
        # OCI: distinguishing VMs with/without containers would require more in-depth analysis
        vm_no_containers = compute_count
        vm_with_containers = 0
        
        tables({"Name": comp['Name'], "Id": comp['Id']}, [
            ["Compute Instances", compute_count]
        ])
        
        result = licensing_count("OCI", vm_no_containers, vm_with_containers, 0, 0, 0, 0, 0, {"Name": comp['Name'], "Id": comp['Id']})
        results.append(result)

    if args.output == "json":
        print(json.dumps(results, indent=2))
    else:
        print_global_summary(results)

# ---------------------------- MAIN ----------------------------
if __name__ == '__main__':
    if args.aws:
        cortex_cloud_metering()
        pcs_sizing_aws()
    elif args.azure:
        cortex_cloud_metering()
        pcs_sizing_az()
    elif args.oci:
        cortex_cloud_metering()
        pcs_sizing_oci()
    elif args.gcp:
        cortex_cloud_metering()
        pcs_sizing_gcp()
    else:
        cortex_cloud_metering()
        print("\nYou must specify a cloud provider:\n--aws | --azure | --gcp | --oci")
