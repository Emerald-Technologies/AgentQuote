# BOM Processor Agent — Deployment Setup Guide

End-to-end walkthrough for deploying the BOM processor as a Foundry Hosted Agent.
All commands are PowerShell (`pwsh`). Run them in order.

---

## Info You Need Before Starting

| Item | Where to get it | Example |
|---|---|---|
| Azure subscription ID | `az account show --query id -o tsv` | `be5a3beb-...` |
| Resource group name | Azure Portal or team lead | `ems-rg-e1` |
| Foundry account name | Chosen by you — must be globally unique | `et-foundry-e2` |
| ACR name | Chosen by you — must be globally unique, alphanumeric only | `etfoundryacr` |
| Storage account name | Chosen by you — must be globally unique, lowercase alphanumeric | `etbomstorage` |
| Deployment region | Must be in the [Hosted Agents supported list](https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents#region-availability) | `eastus2` |
| Azure account with Owner/UAA role | Required for the RBAC steps at the end | — |

> **Note:** East US is NOT supported for Hosted Agents. Supported regions include East US 2, North Central US, Sweden Central, Canada Central, and others. Check the link above for the current list.

---

## Step 1 — Login to Commercial Azure

```powershell
az cloud set --name AzureCloud
az login
az account show --query "{sub:name, id:id}" -o table
```

If you have multiple subscriptions, set the right one:

```powershell
az account set --subscription "<subscription-id>"
```

---

## Step 2 — Create Azure Container Registry

```powershell
az acr create `
  --name <acr-name> `
  --resource-group <resource-group> `
  --sku Basic `
  --location <region> `
  --allow-trusted-services true `
  --query "{name:name, loginServer:loginServer}" -o table
```

---

## Step 2B — Create Storage Account and Blob Containers

This is where customer BOMs, parser scripts, and the canonical format live. The agent reads BOMs from blob storage and writes parsers back to it.

```powershell
# Create the storage account
az storage account create `
  --name <storage-account-name> `
  --resource-group <resource-group> `
  --location <region> `
  --sku Standard_LRS `
  --query "{name:name, location:location}" -o table

# Create the three containers
az storage container create `
  --name boms `
  --account-name <storage-account-name> `
  --auth-mode login

az storage container create `
  --name parsers `
  --account-name <storage-account-name> `
  --auth-mode login

az storage container create `
  --name format `
  --account-name <storage-account-name> `
  --auth-mode login
```

Upload the canonical format reference and any existing sample BOMs:

```powershell
# Upload the canonical schema
az storage blob upload `
  --account-name <storage-account-name> `
  --container-name format `
  --name finalformat.xlsx `
  --file format\finalformat.xlsx `
  --auth-mode login `
  --overwrite

# Upload sample BOMs (repeat per customer)
az storage blob upload `
  --account-name <storage-account-name> `
  --container-name boms `
  --name acme/sample.xlsx `
  --file boms\acme\sample.xlsx `
  --auth-mode login
```

Get the connection string for the agent's environment variable:

```powershell
az storage account show-connection-string `
  --name <storage-account-name> `
  --resource-group <resource-group> `
  --query connectionString -o tsv
```

Save this connection string — you'll need it in Step 7 (agent payload) and Step 9 (.env).

---

## Step 3 — Build and Push the Container Image

Run from the repo root (where the `Dockerfile` lives). Uses ACR Cloud Build — no local Docker needed.

The image tag should be today's date (`yyyyMMdd`) to keep it unique and traceable.

```powershell
Set-Location "C:\path\to\aiFoundrySetup"

az acr build `
  --registry <acr-name> `
  --image bom-processor:20260615 `
  --platform linux/amd64 `
  --file Dockerfile .
```

Verify the image landed:

```powershell
az acr repository show-tags --name <acr-name> --repository bom-processor -o table
```

> **Note:** The Dockerfile uses `mcr.microsoft.com/devcontainers/python:3.12` as the base image to avoid Docker Hub rate limits during ACR cloud builds. Do not change it back to `python:3.12-slim`. The Dockerfile overrides the default `ENTRYPOINT` from this image — the devcontainers base image ships with `/usr/local/share/docker-init.sh` as its entrypoint (a VS Code workspace setup script), which hangs in the Foundry cloud environment and prevents the server from starting.

---

## Step 4 — Create the Foundry AIServices Resource

The resource must be created with a **system-assigned managed identity and custom subdomain in a single call**. If you create the resource first and try to add the identity later, project creation will fail.

Use a JSON file for the body (inline JSON with single quotes causes PowerShell quoting issues):

Create `.foundry/account-create.json`:

```json
{
  "kind": "AIServices",
  "sku": { "name": "S0" },
  "location": "<region>",
  "identity": { "type": "SystemAssigned" },
  "properties": {
    "customSubDomainName": "<foundry-account-name>",
    "publicNetworkAccess": "Enabled"
  }
}
```

Then run:

```powershell
az rest --method PUT `
  --url "https://management.azure.com/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.CognitiveServices/accounts/<foundry-account-name>?api-version=2024-10-01" `
  --body "@.foundry/account-create.json" `
  --query "{name:name, location:location, state:properties.provisioningState, identity:identity.type}" -o table
```

Expected output: `State = Succeeded`, `Identity = SystemAssigned`

> **If you get a "FlagMustBeSetForRestore" error:** The name was previously soft-deleted. Add `"restore": true` inside the `properties` object of `account-create.json` and rerun. You need subscription-level Owner to purge soft-deleted resources — restoring with the flag is the workaround.

---

## Step 5 — Deploy DeepSeek-V4-Pro

```powershell
az cognitiveservices account deployment create `
  --name <foundry-account-name> `
  --resource-group <resource-group> `
  --deployment-name DeepSeek-V4-Pro `
  --model-name DeepSeek-V4-Pro `
  --model-version "2026-04-23" `
  --model-format DeepSeek `
  --sku-name GlobalStandard `
  --sku-capacity 200 `
  --query "{name:name, model:properties.model.name, state:properties.provisioningState}" -o table
```

Check available quota in your target region first:

```powershell
az cognitiveservices usage list --location <region> `
  --query "[?contains(name.value,'DeepSeek-V4-Pro')].{name:name.value, current:currentValue, limit:limit}" -o table
```

---

## Step 6 — Create the Foundry Project (Portal — one-time only)

The project creation API currently requires the portal for initial bootstrapping.

1. Go to **[ai.azure.com](https://ai.azure.com)**
2. Click **+ Create project**
3. Select the existing resource `<foundry-account-name>` from the dropdown
4. Name the project (e.g. `CodingAssistant`) → Create
5. Copy the **project endpoint** from the project settings page

It will look like:
```
https://<foundry-account-name>.services.ai.azure.com/api/projects/<project-name>
```

Verify it is reachable via CLI:

```powershell
az rest --method GET `
  --url "https://<foundry-account-name>.services.ai.azure.com/api/projects/<project-name>/agents?api-version=2025-05-15-preview" `
  --resource "https://ai.azure.com"
```

Expected: `{ "data": [], "has_more": false, "object": "list" }`

---

## Step 7 — Create the Hosted Agent

Create `.foundry/agent-payload.json`:

```json
{
  "name": "bom-processor",
  "definition": {
    "kind": "hosted",
    "image": "<acr-name>.azurecr.io/bom-processor:<image-tag>",
    "cpu": "0.5",
    "memory": "1Gi",
    "container_protocol_versions": [
      { "protocol": "responses", "version": "1.0.0" }
    ],
    "environment_variables": {
      "AZURE_AI_MODEL_DEPLOYMENT_NAME_PRO": "DeepSeek-V4-Pro",
      "AZURE_AI_MODEL_DEPLOYMENT_NAME_FLASH": "DeepSeek-V4-Flash",
      "BOM_STORAGE_CONNECTION_STRING": "<storage-connection-string>"
    }
  }
}
```

> **Important:** Do NOT include `FOUNDRY_PROJECT_ENDPOINT` in `environment_variables` — it is a reserved platform variable and the API will reject it. The platform injects it automatically.

```powershell
az rest --method POST `
  --url "https://<foundry-account-name>.services.ai.azure.com/api/projects/<project-name>/agents?api-version=2025-05-15-preview" `
  --resource "https://ai.azure.com" `
  --body "@.foundry/agent-payload.json"
```

From the response, note these values for Step 8:

| Key | Path in response | Description |
|---|---|---|
| Agent instance identity | `instance_identity.principal_id` | Agent's runtime identity |
| Blueprint principal | `blueprint.principal_id` | Platform infrastructure identity |
| Project identity | see Step 8 below | Pulls the container image from ACR |

Get the project identity:

```powershell
az rest --method GET `
  --url "https://management.azure.com/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.CognitiveServices/accounts/<foundry-account-name>/projects/<project-name>?api-version=2025-04-01-preview" `
  --query "identity.principalId" -o tsv
```

---

## Step 8 — Assign RBAC Roles (requires Owner or User Access Administrator)

Three role assignments are required. If you don't have this permission, send the commands below to your Azure admin.

```powershell
$cogScope = "/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.CognitiveServices/accounts/<foundry-account-name>"
$acrScope = "/subscriptions/<subscription-id>/resourceGroups/<resource-group>/providers/Microsoft.ContainerRegistry/registries/<acr-name>"

# Agent instance identity -> Foundry User (allows agent to call the model)
az role assignment create `
  --assignee "<instance_identity.principal_id>" `
  --role "Foundry User" `
  --scope $cogScope

# Blueprint principal -> Foundry User (platform infrastructure)
az role assignment create `
  --assignee "<blueprint.principal_id>" `
  --role "Foundry User" `
  --scope $cogScope

# Project managed identity -> AcrPull (allows Foundry to pull your container image)
az role assignment create `
  --assignee "<project-identity-principal-id>" `
  --role "AcrPull" `
  --scope $acrScope
```

Also assign `Foundry User` to each team member's Entra ID so they can invoke the agent:

```powershell
az role assignment create `
  --assignee "<team-member-email-or-object-id>" `
  --role "Foundry User" `
  --scope $cogScope
```

---

## Step 9 — Update .env

Update `.env` in the repo root with the new endpoint:

```
FOUNDRY_PROJECT_ENDPOINT="https://<foundry-account-name>.services.ai.azure.com/api/projects/<project-name>"
AZURE_AI_MODEL_DEPLOYMENT_NAME_PRO="DeepSeek-V4-Pro"
AZURE_AI_MODEL_DEPLOYMENT_NAME_FLASH="DeepSeek-V4-Flash"
BOM_STORAGE_CONNECTION_STRING="<storage-connection-string>"
```

---

## Step 10 — Smoke Test

After RBAC is assigned (allow ~2 min for propagation):

```powershell
$token = az account get-access-token --resource "https://ai.azure.com" --query "accessToken" -o tsv
$endpoint = "https://<foundry-account-name>.services.ai.azure.com/api/projects/<project-name>/agents/bom-processor/endpoint/protocols/openai/responses"

# Simple test message
$body = '{"input":"Confirm you are the BOM processor agent and describe your canonical format."}'
Invoke-RestMethod -Uri $endpoint -Method POST `
  -Headers @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" } `
  -Body $body | ConvertTo-Json -Depth 5
```

---

## Team Endpoint

Once deployed, the agent is available at:

```
https://<foundry-account-name>.services.ai.azure.com/api/projects/<project-name>/agents/bom-processor/endpoint/protocols/openai/responses
```

Team members authenticate with `az login` + `DefaultAzureCredential` in code, or via the Foundry Portal at **[ai.azure.com](https://ai.azure.com)**.

---

## Redeployment (code changes)

To push a new version after editing `main.py` or any other source file:

**Step 1 — Build and push a new image**

Use today's date as the tag. If you do multiple builds on the same day, append a letter (`20260702b`, `20260702c`, etc.).

```powershell
Set-Location "C:\Users\Andrew.Shea\OneDrive - Emerald Technologies\Documents\github\agentQuote"

az acr build `
  --registry etfoundryacr2 `
  --image bom-processor:20260702 `
  --platform linux/amd64 `
  --file Dockerfile .
```

Verify the tag was pushed:

```powershell
az acr repository show-tags --name etfoundryacr2 --repository bom-processor -o table
```

**Step 2 — Update the image tag in the payload**

Edit `.foundry/bom-agent-payload.json` and set the `"image"` field to the new tag:

```json
"image": "etfoundryacr2.azurecr.io/bom-processor:20260702"
```

**Step 3 — Deploy a new version**

```powershell
az rest --method POST `
  --url "https://et-foundry-e2.services.ai.azure.com/api/projects/CodingAssistant/agents/bom-processor/versions?api-version=2025-05-15-preview" `
  --resource "https://ai.azure.com" `
  --body "@.foundry/bom-agent-payload.json"
```

Each POST creates a new immutable version. Traffic routes to `@latest` automatically.

**Step 4 — Smoke test**

Wait ~90 seconds for the container to start, then:

```powershell
$token = az account get-access-token --resource "https://ai.azure.com" --query "accessToken" -o tsv
$endpoint = "https://et-foundry-e2.services.ai.azure.com/api/projects/CodingAssistant/agents/bom-processor/endpoint/protocols/openai/responses?api-version=2025-05-15-preview"

Invoke-RestMethod -Uri $endpoint -Method POST `
  -Headers @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" } `
  -Body '{"input":"Confirm you are the BOM processor agent and describe your canonical format."}' `
  | ConvertTo-Json -Depth 5
```

A successful response has `"status": "completed"` and non-empty `"output"`.

If you see `"status": "failed"`, check the portal: **ai.azure.com** → **CodingAssistant** → **Agents** → **bom-processor** → traces for the most recent request.

---

## Current Deployment Values (this repo)

| Setting | Value |
|---|---|
| Subscription | `be5a3beb-686a-4703-aaa8-ceb70667ad77` |
| Resource group | `ems-rg-e1` |
| Foundry account | `et-foundry-e2` (East US 2) |
| Foundry project | `CodingAssistant` |
| ACR | `etfoundryacr2.azurecr.io` |
| Agent name | `bom-processor` |
| Model | `DeepSeek-V4-Flash` GlobalStandard |
| Agent instance identity | `bf29e157-aa51-457f-9e94-6e1d45b4d3c4` |
| Blueprint principal | `bcd30278-80ed-4a70-b15f-a92e38666b5e` |
| Project managed identity | `59a291d9-4ad5-4fe9-9cae-a530bbbd7a86` |
| Storage account | (to be created — see Step 2B) |

---

## Usage Guide — Blob-Storage Batch Workflow

After deploying with the storage account included, here's how the blob-backed workflow works.

### Drop BOMs into storage

Upload customer BOMs to the `boms` container. Use folder names as customer identifiers:

```powershell
# One customer at a time
az storage blob upload `
  --account-name etbomstorage `
  --container-name boms `
  --name acme/sample.xlsx `
  --file "C:\BOMs\acme_q3.xlsx" `
  --auth-mode login

# Or bulk via AzCopy (faster for many files)
azcopy copy "C:\BOMs\*" "https://etbomstorage.blob.core.windows.net/boms/" --recursive
```

### Tell the agent to process everything

```powershell
$token = az account get-access-token --resource "https://ai.azure.com" --query "accessToken" -o tsv
$endpoint = "https://et-foundry-e2.services.ai.azure.com/api/projects/CodingAssistant/agents/bom-processor/endpoint/protocols/openai/responses?api-version=2025-05-15-preview"

$body = @'
{
  "input": "List all BOMs in blob storage, check which customers already have parsers, then process all new customers. For each one: download the BOM, analyze the format, write a parser, validate it, and upload the parser back to blob storage."
}
'@

Invoke-RestMethod -Uri $endpoint -Method POST `
  -Headers @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" } `
  -Body $body | ConvertTo-Json -Depth 5
```

### How the agent processes BOMs (blob flow)

```
1. list_boms("boms")       → discovers "acme/sample.xlsx", "omega/quote.xlsx"
2. list_boms("parsers")     → discovers "acme/parse.py" (skips — already done)
3. download_blob("omega/quote.xlsx", "boms")  → pulls to /tmp in container
4. run_command(pandas inspect) → learns the format
5. write_file("parsers/omega/parse.py") → creates parser in workspace
6. run_command(python parsers/omega/parse.py --input ... --output ...) → validates
7. upload_blob("parsers/omega/parse.py", "parsers") → persists to blob
8. Reports back: what was processed, what was skipped, any errors
```

### Retrieve parsers from blob

```powershell
# List all parsers
az storage blob list --account-name etbomstorage --container-name parsers --query "[].name" -o tsv

# Download a specific parser
az storage blob download `
  --account-name etbomstorage `
  --container-name parsers `
  --name acme/parse.py `
  --file parsers\acme\parse.py `
  --auth-mode login
```

### Run a parser locally

```powershell
pip install pandas openpyxl
python parsers\acme\parse.py --input "C:\BOMs\acme_q3.xlsx" --output "C:\BOMs\acme_q3_normalized.xlsx"
```

### Data flow recap

```
You                         Blob Storage                  Agent Container
 │                             │                              │
 ├── upload BOM ──────────────► boms/acme/sample.xlsx        │
 │                             │                              │
 │                             │    ◄── list_boms("boms") ───┤
 │                             │    ◄── download_blob() ─────┤
 │                             │                              ├── analyze
 │                             │                              ├── write parser
 │                             │                              ├── run & validate
 │                             │    upload_blob() ──────────► parsers/acme/parse.py
 │                             │                              │
 ├── download parser ◄──────── parsers/acme/parse.py         │
 │                             │                              │
 └── run parser locally        │                              │
```

### Recommended prompt patterns

| Goal | Prompt |
|---|---|
| **First run** | `List all BOMs from blob storage and process every customer that doesn't already have a parser. Upload each parser back to blob when done.` |
| **One customer** | `Download boms/acme/sample.xlsx, analyze it, create parsers/acme/parse.py, validate it, and upload the parser.` |
| **Resume batch** | `List existing parsers, then process only customers in boms/ that are missing from parsers/.` |
| **Specific format help** | `Download parsers/acme/parse.py so I can review it.` |
| **Re-parse after fixes** | `Download boms/acme/updated.xlsx, run parsers/acme/parse.py against it, upload the normalized output.` |
