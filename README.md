# Cloud Sizing Tool - Installation & Usage Guide

## 📋 Description
Python script to calculate the number of Cortex Cloud SKUs required for AWS, Azure, GCP, and OCI.

---

## ⚡ Quick start

**One file, one command, any cloud.** Change the provider flag, nothing else.
Works identically from a laptop, from Cloud Shell and from Windows.

```bash
git clone <REPO_URL> && cd cortex-cloud-sizing-calculator

python3 cloud_sizing_updated_v2.py --gcp      # or --aws | --azure | --oci
```

There is **nothing to install first**. The script inspects its own environment
and fixes what it can:

| It detects | It does |
|---|---|
| Missing SDKs | Creates `./venv`, installs **only** the packages that provider needs, restarts itself inside it |
| Already inside a virtualenv | Installs there instead of creating another one |
| Cloud Shell / CloudShell / CI | Detects it and reports it |
| Broken or absent credentials | Stops with the exact `gcloud` / `aws` / `az` / `oci` command to run |
| GCP: no scope given | Auto-detects the organisation and offers the fast org-wide scan |
| GCP: Cloud Asset API disabled | Prints the situation, prints the command, **asks** before running it |
| GCP: missing IAM role | Prints the ready-to-send `add-iam-policy-binding` command |

The scan itself is **read-only and creates nothing** (the single exception,
`--gcp-mode bq`, is opt-in and documented below).

### What a run looks like

```
==> Cortex Cloud sizing - GCP
    environment: Google Cloud Shell
    this scan is read-only; it creates nothing in the tenant

==> Dependencies
    missing for gcp:asset: google.cloud.asset_v1
    installing google-cloud-asset (this takes ~30s) ...
    OK  dependencies installed
    restarting in the prepared environment ...

==> Authentication
    gcloud account: me@example.com
    OK  credentials valid, API calls billed to my-quota-project
    OK  the scanned projects are never charged and never modified

==> Scan plan
    mode asset, scope organizations/123456789
    one organisation-wide Cloud Asset Inventory read; minutes, not hours

==> Cloud Asset Inventory access

    cloudasset.googleapis.com is NOT enabled on my-quota-project.
    It is needed on this ONE project only, never on the scanned projects.
    It is a project setting, not a deployed resource: free and reversible with
      gcloud services disable cloudasset.googleapis.com --project my-quota-project

    gcloud services enable cloudasset.googleapis.com --project my-quota-project

    Run this command now? [y/N]
```

Answer `y` and it continues on its own. Answer `n` and it exits telling you to
run the command yourself — nothing is ever changed without an explicit yes.

### Preflight flags

```bash
python3 cloud_sizing_updated_v2.py --gcp --check          # checks only, no scan
python3 cloud_sizing_updated_v2.py --gcp --yes            # unattended, accept every prompt
python3 cloud_sizing_updated_v2.py --gcp --no-bootstrap   # never install anything
```

`--check` is the one to run before a customer meeting: it validates
credentials, APIs and permissions without touching any data.

### What the customer must grant (GCP organisation scan)

| | |
|---|---|
| IAM | `roles/cloudasset.viewer` on the organisation — one binding, revocable |
| API | `cloudasset.googleapis.com` on **one** quota project, not on the scanned ones |
| Created resources | **None** |

```bash
gcloud organizations add-iam-policy-binding 123456789 \
    --member=user:me@example.com --role=roles/cloudasset.viewer
```

The API enablement is offered by the script. The IAM binding needs an
Organisation Admin, and the script prints it pre-filled when it is missing.

### Procedure to hand to a customer

Copy-paste this into a mail or chat, replacing `<ORG_ID>`, `<REPO_URL>` and
`<QUOTA_PROJECT>`.

> **Prerequisites** — nothing to install. Use Cloud Shell (terminal icon in the
> GCP console) or any machine with `gcloud`. Two grants are needed:
>
> ```bash
> gcloud organizations add-iam-policy-binding <ORG_ID> \
>     --member=user:<YOUR_EMAIL> --role=roles/cloudasset.viewer
> gcloud services enable cloudasset.googleapis.com --project <QUOTA_PROJECT>
> ```
>
> **1. Check the prerequisites** (reads no data):
>
> ```bash
> git clone <REPO_URL> && cd cortex-cloud-sizing-calculator
> python3 cloud_sizing_updated_v2.py --gcp --gcp-scope organizations/<ORG_ID> --check
> ```
>
> **2. Run the scan:**
>
> ```bash
> python3 cloud_sizing_updated_v2.py --gcp --gcp-scope organizations/<ORG_ID> \
>     --output csv --csv-file sizing-org.csv
> ```
>
> It reads Cloud Asset Inventory, the inventory Google already maintains for
> your organisation. A few minutes even at tens of thousands of projects,
> **read-only, no resource created**. Send back `sizing-org.csv`.

Do **not** put `--yes` in what the customer runs: that flag auto-accepts the API
enablement prompt, which contradicts "nothing changes without your approval".

Everything below covers the other clouds, the other GCP modes and manual
invocation.

---

## 🔧 Installation

> **This section is optional.** The script installs its own dependencies on
> first run (see the Quick start above). Use it only if you prefer to control
> the environment yourself, or if you run with `--no-bootstrap`.

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
    google-api-python-client google-auth google-cloud-asset
```

#### GCP, large organisation only (Cloud Asset Inventory mode)
```bash
pip install --user google-cloud-asset
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
python3 cloud_sizing_updated_v2.py --aws

# AWS - Filter by region (e.g., us-*, eu-*, ap-*)
python3 cloud_sizing_updated_v2.py --aws --region-prefix us

# Azure - All subscriptions
python3 cloud_sizing_updated_v2.py --azure

# GCP - All projects (per-project mode)
python3 cloud_sizing_updated_v2.py --gcp

# GCP - Whole organisation, fast, read-only (see "Large organisations" below)
python3 cloud_sizing_updated_v2.py --gcp --gcp-scope organizations/123456789

# OCI - All compartments (recursive)
python3 cloud_sizing_updated_v2.py --oci
```

### Output Formats

```bash
# Table format (default) - shows the top 25 accounts by SKU + the grand total
python3 cloud_sizing_updated_v2.py --azure

# Full table, no truncation
python3 cloud_sizing_updated_v2.py --azure --top 0

# Per-account service-by-service breakdown (verbose; off by default at scale)
python3 cloud_sizing_updated_v2.py --azure --details

# JSON format
python3 cloud_sizing_updated_v2.py --azure --output json

# CSV - recommended when there are hundreds/thousands of accounts
python3 cloud_sizing_updated_v2.py --gcp --gcp-scope organizations/123456789 \
    --output csv --csv-file sizing.csv
```

Progress messages and errors go to **stderr**, results to **stdout**, so
`--output json > out.json` and `--output csv` are always machine-readable.

### Preflight and environment

The same three flags apply to every provider.

| Flag | Effect |
|---|---|
| `--check` | Run the preflight (dependencies, credentials, APIs, permissions) and stop before scanning |
| `--yes` / `-y` | Answer yes to every prompt — required for cron, CI and other non-interactive runs |
| `--no-bootstrap` | Never create a venv and never install anything; fail with the `pip install` command instead |

```bash
# Validate the environment before a customer session
python3 cloud_sizing_updated_v2.py --gcp --check

# Unattended run in CI, results to a file
python3 cloud_sizing_updated_v2.py --gcp --gcp-scope organizations/123456789 \
    --yes --output csv --csv-file sizing.csv
```

Without a TTY the script never blocks: every prompt is treated as "no" unless
`--yes` is passed, so an unattended run either succeeds or exits with the exact
command a human has to run.

---

## 🏔️ Large organisations (thousands of GCP projects)

The per-project loop issues ~8 API calls per project, sequentially. On an
organisation with tens of thousands of projects that is tens of hours, it trips
per-project API-enablement gaps, and any interruption loses everything.

`--gcp-scope` switches the collection to **Cloud Asset Inventory**: one
organisation-wide read instead of `8 × N` per-project calls.

```bash
# Whole organisation
python3 cloud_sizing_updated_v2.py --gcp --gcp-scope organizations/123456789 \
    --output csv --csv-file sizing.csv

# A single folder / business unit
python3 cloud_sizing_updated_v2.py --gcp --gcp-scope folders/987654321
```

### Collection modes (`--gcp-mode`)

| Mode | What it does | Resources created in the tenant | Good for |
|------|--------------|--------------------------------|----------|
| `auto` *(default)* | `asset` if `--gcp-scope` is an org/folder, else `project` | — | — |
| `asset` | `cloudasset.searchAllResources` at org/folder scope, one parallel stream per asset type | **None** | Thousands of projects |
| `project` | Original per-project API calls, now parallel + resumable | **None** | A handful of projects, or no org-level access |
| `bq` | Cloud Asset Inventory export to BigQuery + one SQL aggregation | **Ephemeral dataset**, see below | Very large estates where `asset` pagination is the bottleneck |

### Does it create anything at the customer? — `asset` mode: no

`asset` mode is **strictly read-only**. `searchAllResources` is a query API; it
writes nothing, stores nothing and leaves no trace beyond audit-log entries.

Two prerequisites, neither of which is a resource:

1. **`roles/cloudasset.viewer` on the scope** (organisation or folder). An IAM
   binding, revocable in one click.
2. **`cloudasset.googleapis.com` enabled on the quota project** — the project
   your credentials bill the API call to, *not* the 40 000 scanned projects.
   Enabling an API is a project setting, not a deployed resource; it is free and
   reversible with `gcloud services disable cloudasset.googleapis.com`.

```bash
gcloud services enable cloudasset.googleapis.com --project MY-QUOTA-PROJECT
gcloud organizations add-iam-policy-binding 123456789 \
    --member=user:me@example.com --role=roles/cloudasset.viewer
```

Note that this replaces the long per-project role list further down: CAI reports
assets even in projects where `compute.googleapis.com` & co. are not enabled, so
no per-project API enablement or per-project IAM grant is needed.

### `bq` mode and its ephemeral resources

Only this mode writes to the tenant, and only for the duration of the scan.

```bash
python3 cloud_sizing_updated_v2.py --gcp --gcp-mode bq \
    --gcp-scope organizations/123456789 --bq-project my-sandbox-project \
    --output csv --csv-file sizing.csv
```

What is created, and how it is guaranteed to disappear:

- **One BigQuery dataset** `cortex_sizing_tmp_<random>` in `--bq-project` (never
  in the scanned projects), holding a single `assets` table.
- Deleted in a `finally` block at the end of the scan.
- Deleted on `Ctrl-C` (SIGINT) and SIGTERM, and at interpreter exit.
- The dataset is created with a **6-hour default table expiration**, so even a
  `kill -9` or a machine crash leaves nothing behind: BigQuery reclaims it.
- The dataset id is recorded in `.cortex_sizing_bq_state.json`; if the deletion
  ever fails, the script prints the exact `bq rm` command, and:

```bash
# Remove a leftover dataset from an interrupted run
python3 cloud_sizing_updated_v2.py --gcp --bq-cleanup-only
```

Extra permission for this mode: `roles/bigquery.user` +
`roles/bigquery.dataEditor` (or `bigquery.datasets.create`/`delete`) on
`--bq-project` only.

### Parallelism — what `--workers` actually does

`--workers` (default 16) means different things per mode, and in `asset` mode
raising it does nothing:

| Mode | Unit of parallelism | Effective cap |
|---|---|---|
| `asset` | one stream per asset type | **10** (11 with `--count-images`) — `min(--workers, len(asset_types))` |
| `project` | one thread per project | `--workers`, useful up to a few dozen |
| AWS / Azure / OCI | accounts, subscriptions, regions | `--workers` |

So `--workers 64` on an organisation scan behaves exactly like `--workers 10`.

**To go faster than that, split the scope, not the workers** — run one scan per
top-level folder concurrently:

```bash
for f in $(gcloud resource-manager folders list --organization=123456789 --format='value(ID)'); do
    python3 cloud_sizing_updated_v2.py --gcp --gcp-scope folders/$f \
        --output csv --csv-file sizing-folder-$f.csv &
done
wait
```

Two caveats before doing this:

- Top-level folders do **not** cover projects attached directly to the org root.
  Scan those separately or they silently vanish from the total.
- Concatenate the CSVs and **deduplicate by `account_id`**: a project reachable
  from two scopes would otherwise be counted twice.

Start with the single org-wide scan. It handles 45 000 projects; the split is a
fallback, not the default.

### Other reliability properties

- **Backoff**: automatic retry with exponential backoff on quota (429) and 5xx
  in `asset` mode; adaptive retries on AWS.
- **Partial-failure isolation**: in `asset` mode each asset type is its own
  stream, so a permission gap on one type reports a warning and degrades that
  line only, instead of aborting the run.
- **Honest totals**: in `project` mode, unreadable APIs are aggregated into a
  PARTIAL COVERAGE report and the total is labelled `INCOMPLETE`, so an
  under-count is never mistaken for a result.
- **Resume**: `project` mode supports `--checkpoint FILE`; re-running skips
  projects already recorded.

```bash
python3 cloud_sizing_updated_v2.py --gcp --gcp-mode project --checkpoint scan.jsonl --workers 32
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
TOTAL: 971 Cortex Cloud workload(s) (SKU) needed for Azure
============================================================

========================================================================================================================
                              GLOBAL SKU SUMMARY - ALL ACCOUNTS/SUBSCRIPTIONS/PROJECTS                                
========================================================================================================================
Cloud           Account/Subscription/Project ID               Account Name                        SKU       
------------------------------------------------------------------------------------------------------------------------
Azure           xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx         Sub1_prod                            715       
Azure           xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx         Sub2_Shared_Services                 256         
------------------------------------------------------------------------------------------------------------------------

Accounts scanned                                                                                  2         
Accounts with 0 SKU                                                                               0         
GRAND TOTAL                                                                                       971       
========================================================================================================================
```

Rows are sorted by SKU descending and truncated to `--top` (default 25); the
grand total always covers every account. Use `--top 0` or `--output csv` for the
full list.

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
- Compute Instances, `RUNNING` or `TERMINATED`, **excluding GKE nodes** (VMs without containers)
- GKE Nodes — Compute Instances carrying the `goog-gke-node` label (VMs with containers)
- Cloud Run Services (CaaS), **excluding** services backing a gen2 Cloud Function
- Cloud Functions gen1 + gen2 (Serverless)
- Cloud Storage Buckets
- BigQuery Datasets (PaaS DB)
- Bigtable Instances (PaaS DB)
- Cloud SQL Instances, excluding stopped/suspended/failed (PaaS DB)
- Artifact Registry images — only with `--count-images` (informational, never billed here)

### OCI
- Compute Instances, `RUNNING` or `STOPPED`, across **all** compartments (recursive)

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

### GCP — `--gcp-mode asset` (recommended for organisations)
- `roles/cloudasset.viewer` on the organisation or folder — **that is all**
- `cloudasset.googleapis.com` enabled on the quota project only

### GCP — `--gcp-mode project` (per-project)
- Compute Viewer
- Kubernetes Engine Viewer
- Cloud Functions Viewer
- BigQuery User
- Storage Object Viewer
- Cloud SQL Viewer
- …on **every** project, plus the matching API enabled on every project.
  This is why `asset` mode is preferred beyond a few dozen projects.

### GCP — `--gcp-mode bq` (additionally)
- `roles/bigquery.user` + `roles/bigquery.dataEditor` on `--bq-project`

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

In `--gcp-mode asset` this does not apply: Cloud Asset Inventory reports assets
regardless of which APIs are enabled on the scanned projects. Only
`cloudasset.googleapis.com` on the quota project is required.

In `--gcp-mode project`, every API must be enabled on every project. Rather than
dumping one protobuf error per project per service, the script aggregates the
failures by root cause and prints a **partial-coverage report** before the
summary:

```
----------------------------------------------------------------------
PARTIAL COVERAGE - the counts below are a FLOOR, not a total
----------------------------------------------------------------------
  Bigtable           API disabled           9/9 project(s) not counted
                     gcloud services enable bigtableadmin.googleapis.com --project my-quota-project
  Cloud Run          API disabled           5/9 project(s) not counted
                     enable run.googleapis.com on each: proj-a, proj-b, proj-c, +2 more
  Cloud Storage      permission denied      7/9 project(s) not counted
                     e.g. proj-a, proj-b, proj-c, +4 more
----------------------------------------------------------------------
```

Note the distinction: admin APIs such as Bigtable bill the **quota project**, so
one `gcloud services enable` fixes all projects at once. Others must be enabled
per project. The grand total is then labelled `GRAND TOTAL (INCOMPLETE)` so a
partially-blind scan is never mistaken for a full one.

```bash
# Enable required APIs on a given project
gcloud services enable compute.googleapis.com container.googleapis.com \
    cloudfunctions.googleapis.com run.googleapis.com storage-api.googleapis.com \
    bigquery.googleapis.com bigtableadmin.googleapis.com sqladmin.googleapis.com \
    --project MY-PROJECT
```

Doing this across thousands of projects is impractical, which is the real reason
to prefer `--gcp-scope organizations/<ID>`.

### Installation Errors

`ModuleNotFoundError` should no longer happen: the script probes its own
imports before scanning and installs what is missing. If you hit one anyway:

```bash
# Let the script rebuild its environment from scratch
rm -rf venv && python3 cloud_sizing_updated_v2.py --gcp --check
```

A venv is **not relocatable** — `venv/bin/activate` hardcodes its own path. If
you moved or renamed the directory, delete `venv` and let the script recreate
it rather than trying to repair it.

### Installed but unusable (Cloud Shell, shared images)

```
AttributeError: module 'lib' has no attribute 'GEN_EMAIL'
  ... OpenSSL/crypto.py, imported from oauth2client, from googleapiclient
```

A stale `pyOpenSSL` in `~/.local` shadowing the system one and no longer
matching its `cryptography`. Nothing to do with this script, but it breaks any
`googleapiclient` import — so `--gcp-mode project` dies on it.

The preflight detects it (it imports each module for real rather than only
checking that the file exists) and offers a virtualenv, which ignores
`~/.local` entirely and is the reliable way out. Accept the prompt, or:

```bash
python3 -m pip install --user --upgrade pyOpenSSL cryptography
```

Note that `--gcp-mode asset` never touches `googleapiclient` at all, so an
organisation scan is unaffected.

```bash
# Permission denied (only when installing manually)
ERROR: Could not install packages due to an OSError: [Errno 13] Permission denied
→ Use --user flag: pip install --user -r requirements.txt
→ Or let the script create ./venv for you

# externally-managed-environment (Debian/Ubuntu/Homebrew Python, PEP 668)
error: externally-managed-environment
→ This is why the script defaults to a venv. Just run it without --no-bootstrap.
```

## 📝 Notes

- The tool counts **Running/Stopped** VM resources
- Container images benefit from a free quota (10 scans per workload)
- For AWS, the script can scan organization accounts (assume role)
- Resource counts are real-time snapshots at execution time

### Counting corrections vs. the previous version

These changed the numbers, in most cases downwards. Re-run before reusing an
older quote.

| Fix | Effect |
|-----|--------|
| **GCP: GKE nodes were counted twice** — once as Compute Instances, once via `currentNodeCount`. Now split on the `goog-gke-node` label. | Over-count removed; can be large on GKE-heavy estates |
| **GCP: `GCR images = buckets × 5`** was a fabricated number. Now reports "not collected" unless `--count-images` (real Artifact Registry count). | No more invented figures |
| **GCP: Cloud Functions gen2** were counted twice (as a function *and* as the Cloud Run service backing it). Now deduplicated on `goog-managed-by=cloudfunctions`. | Over-count removed |
| **Azure: `ACR images += 10` per registry** was an estimate. Now reports registry count, images uncollected. | No more invented figures |
| **Azure: AKS node VMs** in `MC_*` resource groups are no longer counted both as VMs and as AKS nodes. | Over-count removed |
| **AWS: unpaginated list calls** (`list_clusters`, `list_tasks`, `list_functions`, `describe_db_instances`, `list_tables`, `describe_file_systems`, `list_images`, `describe_instances`) silently read only the first page. Now fully paginated. | **Under-count fixed — numbers go up** |
| **AWS: the payer account was scanned twice** (ambient credentials + org loop). | Duplicate row removed |
| **OCI: only direct child compartments, first page only.** Now recursive and paginated. | **Under-count fixed** |
| **Errors printed to stdout** corrupted `--output json`. Now on stderr. | JSON/CSV always parseable |
| **`import boto3` at module level** made `--gcp` fail without boto3 installed. Now lazy per provider. | Per-CSP installs work |

## 🎯 Best Practices

### Performance
- **GCP with more than ~50 projects: use `--gcp-scope organizations/<ID>`.** It is
  the single biggest win and it creates nothing in the tenant.
- Use `--region-prefix` for AWS to limit region scanning
- Tune `--workers` (default 16); lower it if you hit API quotas
- Use `--output csv` on large estates; the table view truncates to `--top` rows
- Leave `--count-images` off unless the image count is actually needed
- Use `--checkpoint` in `project` mode so an interruption does not restart from zero

### Security
- Use read-only service accounts/principals
- Rotate credentials regularly
- Use least-privilege IAM policies
- Store credentials securely (never in code)

### Automation

Always pass `--yes` in cron/CI: without a TTY every prompt otherwise defaults
to "no", and the run stops at the first thing it would have asked about.

```bash
# Schedule daily runs with cron
0 2 * * * /path/to/venv/bin/python3 /path/to/cloud_sizing_updated_v2.py --azure --yes --output json > /var/log/sizing_$(date +\%Y\%m\%d).json
```

Run once interactively first so the venv exists and the APIs are enabled; then
point cron at `venv/bin/python3` and the bootstrap becomes a no-op.

## 🔄 Multi-Cloud Usage

The provider is just a flag — one script covers all four. To scan several in
sequence:

```bash
# Scan all configured clouds
python3 cloud_sizing_updated_v2.py --aws
python3 cloud_sizing_updated_v2.py --azure
python3 cloud_sizing_updated_v2.py --gcp
python3 cloud_sizing_updated_v2.py --oci

# Or create a wrapper script
#!/bin/bash
for cloud in aws azure gcp oci; do
    python3 cloud_sizing_updated_v2.py --${cloud} --yes --output json > ${cloud}_sizing.json
done
```

Exactly one provider flag per run: passing two exits with an error, passing
none prints the licensing metrics and the usage line.

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
