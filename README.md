# Cloud Sizing Tool - Installation & Usage Guide

## 📋 Description
Python script to calculate the number of Cortex Cloud SKUs required for AWS, Azure, GCP, and OCI.

## 🔧 Installation

### Option 1: Complete Installation (all CSPs)
```bash
pip install --user -r requirements.txt
```

### Option 2: Install by CSP

#### AWS only
```bash
pip install --user boto3 botocore
```

#### Azure only
```bash
pip install --user azure-identity azure-mgmt-compute azure-mgmt-containerservice \
    azure-mgmt-subscription azure-mgmt-web azure-mgmt-sql azure-mgmt-cosmosdb \
    azure-mgmt-storage azure-mgmt-containerregistry
```

#### GCP only
```bash
pip install --user google-cloud-compute google-cloud-container google-cloud-functions \
    google-cloud-bigquery google-cloud-bigtable google-cloud-storage \
    google-api-python-client google-auth
```

#### OCI only
```bash
pip install --user oci
```

### Option 3: Virtual Environment (recommended)
```bash
# Create virtual environment
python3 -m venv venv

# Activate environment
source venv/bin/activate  # Linux/Mac
# or
venv\Scripts\activate     # Windows

# Install dependencies
pip install -r requirements.txt
```

## 🔐 Configuration by CSP

### AWS
```bash
# Option 1: AWS CLI
aws configure

# Option 2: Environment variables
export AWS_ACCESS_KEY_ID="your-access-key"
export AWS_SECRET_ACCESS_KEY="your-secret-key"
export AWS_DEFAULT_REGION="us-east-1"

# Option 3: Credentials file
# ~/.aws/credentials
[default]
aws_access_key_id = YOUR_ACCESS_KEY
aws_secret_access_key = YOUR_SECRET_KEY
```

### Azure
```bash
# Option 1: Azure CLI (recommended)
az login

# Option 2: Service Principal
az login --service-principal -u <app-id> -p <password> --tenant <tenant-id>

# Option 3: Environment variables
export AZURE_SUBSCRIPTION_ID="your-subscription-id"
export AZURE_TENANT_ID="your-tenant-id"
export AZURE_CLIENT_ID="your-client-id"
export AZURE_CLIENT_SECRET="your-client-secret"
```

### GCP
```bash
# Option 1: gcloud CLI (recommended)
gcloud auth application-default login

# Option 2: Service Account
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service-account-key.json"

# Option 3: Create service account key
# 1. GCP Console > IAM & Admin > Service Accounts
# 2. Create service account with roles:
#    - Compute Viewer
#    - Kubernetes Engine Viewer
#    - Cloud Functions Viewer
#    - BigQuery User
#    - Storage Object Viewer
# 3. Download JSON key
```

### OCI
```bash
# Configure ~/.oci/config file
oci setup config

# ~/.oci/config file should contain:
[DEFAULT]
user=ocid1.user.oc1..xxx
fingerprint=xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx:xx
tenancy=ocid1.tenancy.oc1..xxx
region=us-ashburn-1
key_file=~/.oci/oci_api_key.pem
```

## 🚀 Usage

### Basic Commands

```bash
# AWS - All organization accounts
python3 cloud_sizing_updated.py --aws

# AWS - Filter by region (e.g., us-*, eu-*, ap-*)
python3 cloud_sizing_updated.py --aws --region-prefix us

# Azure - All subscriptions
python3 cloud_sizing_updated.py --azure

# GCP - All projects
python3 cloud_sizing_updated.py --gcp

# OCI - All compartments
python3 cloud_sizing_updated.py --oci
```

### Output Formats

```bash
# Table format (default)
python3 cloud_sizing_updated.py --azure

# JSON format
python3 cloud_sizing_updated.py --azure --output json
```

## 📊 Cortex Cloud Licensing Metrics

| Workload Type                    | Billable Units                |
|----------------------------------|-------------------------------|
| VMs not running containers       | 1 VM                          |
| VMs running containers           | 1 VM                          |
| CaaS                             | 10 Managed Containers         |
| Serverless Functions             | 25 Serverless Functions       |
| Cloud Buckets                    | 10 Cloud Buckets              |
| Managed Cloud Database (PaaS)    | 2 PaaS Databases              |
| DBaaS TB stored                  | 1 TB Stored                   |
| SaaS users                       | 10 SaaS Users                 |
| Cloud ASM - service              | 4 Unmanaged Assets            |
| Container Images in Registries   | Free: 10 scans per workload   |

## 📈 Sample Output

```
============================================================
TOTAL: 715 Cortex Cloud workload(s) (SKU) needed for Azure
============================================================

========================================================================================================================
                              GLOBAL SKU SUMMARY - ALL ACCOUNTS/SUBSCRIPTIONS/PROJECTS                                
========================================================================================================================
Cloud           Account/Subscription/Project ID               Account Name                        SKU       
------------------------------------------------------------------------------------------------------------------------
Azure           8d7027d7-8249-474a-a2f4-c1341977d1db         EMEAL_Systems_Engineering           715       
Azure           218f0c22-5366-444b-8897-3467ceb096ab         Shared_Services                     0         
------------------------------------------------------------------------------------------------------------------------

GRAND TOTAL                                                                                       715       
========================================================================================================================
```

## 🔍 Resources Counted by CSP

### AWS
- EC2 Instances (VMs without containers)
- EKS Nodes (VMs with containers)
- ECS Tasks (CaaS)
- Lambda Functions (Serverless)
- S3 Buckets
- RDS Instances (PaaS DB)
- DynamoDB Tables (PaaS DB)
- EFS Systems (PaaS DB)
- ECR Container Images

### Azure
- VMs (without containers)
- AKS Nodes (with containers)
- Azure Container Instances (CaaS)
- Azure Functions (Serverless)
- Storage Accounts (Buckets)
- Azure SQL Databases (PaaS DB)
- Cosmos DB (PaaS DB)
- ACR Container Images

### GCP
- Compute Instances (without containers)
- GKE Nodes (with containers)
- Cloud Run Services (CaaS)
- Cloud Functions (Serverless)
- Cloud Storage Buckets
- BigQuery Datasets (PaaS DB)
- Bigtable Instances (PaaS DB)
- Cloud SQL Instances (PaaS DB)
- GCR/Artifact Registry Images

### OCI
- Compute Instances

## 🛠️ Required Permissions

### AWS
- `ec2:DescribeRegions`
- `ec2:DescribeInstances`
- `ecs:ListClusters`, `ecs:ListTasks`, `ecs:ListTaskDefinitions`
- `lambda:ListFunctions`
- `s3:ListAllMyBuckets`
- `rds:DescribeDBInstances`
- `dynamodb:ListTables`
- `efs:DescribeFileSystems`
- `ecr:DescribeRepositories`, `ecr:ListImages`
- `organizations:ListAccounts` (for multi-account)
- `sts:AssumeRole` (for multi-account)

### Azure
- Reader or Contributor role on subscriptions

### GCP
- Compute Viewer
- Kubernetes Engine Viewer
- Cloud Functions Viewer
- BigQuery User
- Storage Object Viewer
- Cloud SQL Viewer

### OCI
- Compute Inspector or higher

## ❗ Troubleshooting

### Permission Errors
```bash
# AWS
ERROR: An error occurred (UnauthorizedOperation)
→ Check IAM permissions

# Azure
ERROR: (AuthorizationFailed)
→ Check RBAC roles (az login)

# GCP
ERROR: 403 Forbidden
→ Check IAM roles and enable APIs
```

### APIs Not Enabled (GCP)
```bash
# Enable required APIs
gcloud services enable compute.googleapis.com
gcloud services enable container.googleapis.com
gcloud services enable cloudfunctions.googleapis.com
gcloud services enable bigquery.googleapis.com
gcloud services enable storage-api.googleapis.com
```

### Installation Errors
```bash
# Permission denied
ERROR: Could not install packages due to an OSError: [Errno 13] Permission denied
→ Use --user flag: pip install --user -r requirements.txt
→ Or use virtual environment (recommended)

# Module not found after installation
ERROR: ModuleNotFoundError: No module named 'azure'
→ Ensure you're in the correct Python environment
→ Check: pip show azure-identity
→ Use: python3 -m pip install --user -r requirements.txt
```

## 📝 Notes

- The script only counts **active/running** resources
- Container images benefit from a free quota (10 scans per workload)
- For AWS, the script can scan organization accounts (assume role)
- Container image estimates are approximate (ACR, GCR)
- Resource counts are real-time snapshots at execution time

## 🎯 Best Practices

### Performance
- Use `--region-prefix` for AWS to limit region scanning
- Run during off-peak hours for large environments
- Cache credentials to avoid repeated authentication

### Security
- Use read-only service accounts/principals
- Rotate credentials regularly
- Use least-privilege IAM policies
- Store credentials securely (never in code)

### Automation
```bash
# Schedule daily runs with cron
0 2 * * * /usr/bin/python3 /path/to/cloud_sizing_updated.py --azure --output json > /var/log/sizing_$(date +\%Y\%m\%d).json
```

## 🔄 Multi-Cloud Usage

To scan multiple cloud providers in sequence:

```bash
# Scan all configured clouds
python3 cloud_sizing_updated.py --aws
python3 cloud_sizing_updated.py --azure
python3 cloud_sizing_updated.py --gcp
python3 cloud_sizing_updated.py --oci

# Or create a wrapper script
#!/bin/bash
for cloud in aws azure gcp oci; do
    python3 cloud_sizing_updated.py --${cloud} --output json > ${cloud}_sizing.json
done
```

## 📊 Reporting

### Generate Excel Report
```python
# Convert JSON output to Excel
import pandas as pd
import json

with open('azure_sizing.json') as f:
    data = json.load(f)

df = pd.DataFrame(data)
df.to_excel('azure_sizing_report.xlsx', index=False)
```

### Create Summary Dashboard
Combine outputs from multiple clouds to create a unified dashboard showing:
- Total SKU count across all clouds
- Breakdown by cloud provider
- Cost projections
- Growth trends over time

## 🤝 Support

For questions or issues:
1. Check cloud account permissions
2. Verify APIs are enabled (GCP)
3. Verify authentication configuration
4. Review script error logs
5. Check cloud provider service status

## 📄 License

This tool is provided as-is for Cortex Cloud sizing purposes.

## 🔗 Related Documentation

- [AWS API Documentation](https://docs.aws.amazon.com/index.html)
- [Azure SDK Documentation](https://docs.microsoft.com/en-us/azure/)
- [GCP Client Libraries](https://cloud.google.com/python/docs/reference)
- [OCI SDK Documentation](https://docs.oracle.com/en-us/iaas/tools/python/latest/)
- [Cortex Cloud Documentation](https://docs.paloaltonetworks.com/)
