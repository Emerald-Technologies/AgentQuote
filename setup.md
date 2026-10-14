# BOM Processor Agent — Deployment Setup Guide

End-to-end walkthrough for deploying the BOM processor. Two paths are documented:

1. **Azure Government** — hosted on **Container Apps** (Hosted Agents are not available in Gov regions)
2. **Commercial Azure** — hosted on **Foundry Hosted Agents** (original walkthrough)

All commands are PowerShell (`pwsh`). Run them in order.

---

# Part A — Azure Government Deployment (Container Apps)

> **Quick Quote integration security gate:** The prompt-driven Responses app
> documented below remains separate from the deterministic BOM processing API.
> Do not route customer BOM processing through `/responses`. Deploy
> `deterministic_api.py` as a separate Container App, require Container Apps
> Authentication, restrict callers to the Quick Quote workload identity, and
> block any ingress path that can bypass authentication. Do not deploy the
> deterministic service until parser execution has a reviewed workload sandbox.

## Deterministic API configuration

The separate service listens on port `8088` and exposes only:

```text
POST /api/v1/bom/process
```

A review template is provided at
`.foundry/deterministic-containerapp-gov.yaml`. It uses internal ingress,
managed identities, bounded replicas/resources, and placeholders rather than
credentials. It deliberately does **not** configure Easy Auth or claim to
sandbox parsers. Run `dev/Test-DeterministicDeploymentReadiness.ps1` for local,
non-Azure checks.

The existing `Dockerfile`, `/responses` smoke test, and `bom-processor-gov`
commands below deploy the prompt-driven agent—not this deterministic API. Build
the deterministic image from `Dockerfile.deterministic` under a distinct image
and Container App name. Do not replace the existing app until both services'
ownership and migration plan are approved.

Required production settings:

```dotenv
BOM_STORAGE_ACCOUNT_URL=https://etbomstoragegov.blob.core.usgovcloudapi.net
# Optional for a user-assigned identity:
# BOM_STORAGE_MANAGED_IDENTITY_CLIENT_ID=<client-id>
BOM_API_AUTH_MODE=easy_auth
BOM_API_ALLOWED_APP_IDS=<quick-quote-workload-app-id>
# Optional additional restriction:
BOM_API_ALLOWED_OBJECT_IDS=<quick-quote-service-principal-object-id>
BOM_API_MAX_BODY_BYTES=131072
BOM_API_MAX_CONCURRENT_JOBS=2
```

Container Apps Authentication must validate the Entra token before forwarding
the `X-MS-CLIENT-PRINCIPAL` identity header. The application allowlist is defense
in depth; the header is spoofable if public traffic can reach the container
without passing through authenticated ingress.

Azure configuration must also provide:

- private/internal ingress or an approved authenticated public ingress;
- least-privilege Blob RBAC or the existing storage secret until managed
  identity storage access is configured (production should use managed identity);
- no network egress for parser execution;
- non-root execution and CPU, memory, process, and filesystem limits;
- no storage credentials or unrelated secrets in the parser process environment;
- log redaction for auth headers, request bodies, Blob paths, and credentials.

The code intentionally does not perform direct JWT validation. That would need
approved Government-cloud issuer/audience rules, JWKS rotation, and an additional
security-reviewed dependency. Easy Auth is the agreed implementation boundary
for this increment.

Easy Auth must be configured and independently verified after creating the app.
With an approved tenant, application registration, issuer, and audience, use the
locally installed Container Apps CLI's `az containerapp auth microsoft update`
and `az containerapp auth update` commands. Require HTTPS and set unauthenticated
requests to `Return401`; do not add excluded paths. Then verify the effective
settings with `az containerapp auth show`. Exact Government issuer and audience
values are deployment inputs and must not be guessed or copied from commercial
Azure examples.

## Unresolved parser isolation gate

`shell=False`, temporary directories, and timeouts reduce accidental failures
but do not isolate Python parser code. Before deployment, approve and validate a
separate short-lived parser worker/job with no unrelated secrets, no network
egress, a non-root identity, a disposable filesystem, and CPU/memory/process
limits. Until that boundary exists, only reviewed trusted parser code may be
used and production customer BOM processing remains blocked.

> **Why Container Apps?** Foundry Hosted Agents are not enabled in Azure Government regions. The agent container is self-contained (a Responses API server on port 8088), so it runs unchanged in a Container App. The only code difference is a Gov scope fix in `main.py` (see note below).

## Gov Info You Need Before Starting

| Item | Value used |
|---|---|
| Azure cloud | `AzureUSGovernment` |
| Subscription | `37d47e73-56ae-433b-a340-86eac088c78e` (Azure subscription ITAR) |
| Resource group | `et-rg-adf` (usgovvirginia) |
| ACR | `etContainerRegistry` (`etcontainerregistry.azurecr.us`) |
| Model | `gpt-5.6-luna` v2026-07-09, DataZoneStandard, capacity 10 (10,000 TPM) |
| Foundry project | `AzureGovAgents` on account `azuregovagents-resource` |
| Storage account | `etbomstoragegov` |
| Container App | `bom-processor-gov` in env `et-bom-ca-env` |

## Gov Step 1 — Login to Azure Government

```powershell
az cloud set --name AzureUSGovernment
az login
az account show --query "{name:name, id:id}" -o table
```

## Gov Step 2 — Create the Foundry AIServices Resource

Create `.foundry/account-create-gov.json`:

```json
{
  "kind": "AIServices",
  "sku": { "name": "S0" },
  "location": "usgovvirginia",
  "identity": { "type": "SystemAssigned" },
  "properties": {
    "customSubDomainName": "<foundry-account-name>",
    "publicNetworkAccess": "Enabled"
  }
}
```

Then run (note the `.us` management endpoint):

```powershell
az rest --method PUT `
  --url "https://management.usgovcloudapi.net/subscriptions/<subscription-id>/resourceGroups/et-rg-adf/providers/Microsoft.CognitiveServices/accounts/<foundry-account-name>?api-version=2024-10-01" `
  --resource "https://management.usgovcloudapi.net" `
  --body "@.foundry/account-create-gov.json" `
  --query "{name:name, location:location, state:properties.provisioningState, identity:identity.type}" -o table
```

> **Important:** When you create the project in the portal (ai.azure.us), the portal may create a **second** AIServices account for the project. Check `az cognitiveservices account list -g et-rg-adf` after this step. The model deployment and RBAC must target the account that actually hosts the project (in our case `azuregovagents-resource`).

## Gov Step 3 — Deploy gpt-5.6-luna

Create `.foundry/luna-deployment-gov.json` (get the exact version from the portal or `list-models`):

```json
{
  "sku": { "name": "DataZoneStandard", "capacity": 10 },
  "properties": {
    "model": {
      "format": "OpenAI",
      "name": "gpt-5.6-luna",
      "version": "2026-07-09"
    }
  }
}
```

```powershell
az rest --method PUT `
  --url "https://management.usgovcloudapi.net/subscriptions/<subscription-id>/resourceGroups/et-rg-adf/providers/Microsoft.CognitiveServices/accounts/<project-hosting-account>/deployments/gpt-5.6-luna?api-version=2024-10-01" `
  --resource "https://management.usgovcloudapi.net" `
  --body "@.foundry/luna-deployment-gov.json" `
  --query "{name:name, sku:sku.name, capacity:sku.capacity, state:properties.provisioningState}" -o table
```

## Gov Step 4 — Create the Foundry Project (Portal)

1. Go to **ai.azure.us** → **+ Create project**
2. Select the AIServices resource → name it (e.g. `AzureGovAgents`)
3. Copy the project endpoint: `https://<account>.services.ai.azure.us/api/projects/<project>`

## Gov Step 5 — Build and Push the Container Image

```powershell
az acr build `
  --registry etContainerRegistry `
  --image bom-processor:<tag> `
  --platform linux/amd64 `
  --file Dockerfile .
```

## Gov Step 6 — Create Storage Account and Containers

```powershell
az storage account create --name etbomstoragegov --resource-group et-rg-adf `
  --location usgovvirginia --sku Standard_LRS

foreach ($c in 'boms','parsers','format') {
  az storage container create --name $c --account-name etbomstoragegov --auth-mode login
}

az storage account show-connection-string --name etbomstoragegov `
  --resource-group et-rg-adf --query connectionString -o tsv
```

## Gov Step 7 — Create the Container App

Create a user-assigned identity for ACR image pulls, then create the app from a YAML spec (`.foundry/bom-containerapp-gov.yaml`):

```powershell
az identity create --name id-bom-processor-gov --resource-group et-rg-adf --location usgovvirginia

# Give the pull identity AcrPull on the Gov ACR
$acrScope = "/subscriptions/<subscription-id>/resourceGroups/et-rg-adf/providers/Microsoft.ContainerRegistry/registries/etContainerRegistry"
az role assignment create --assignee "<pull-identity-principal-id>" --role "AcrPull" --scope $acrScope

az containerapp env create --name et-bom-ca-env --resource-group et-rg-adf --location usgovvirginia

az containerapp create --name bom-processor-gov --resource-group et-rg-adf `
  --yaml .foundry/bom-containerapp-gov.yaml
```

The YAML sets:
- Both `UserAssigned` (ACR pull) and `SystemAssigned` (model auth) identities
- ACR registry auth via the pull identity
- Env vars: `FOUNDRY_PROJECT_ENDPOINT`, `AZURE_AI_MODEL_DEPLOYMENT_NAME_FLASH`, `BOM_STORAGE_CONNECTION_STRING` (as a secret)
- Ingress on target port **8088**

## Gov Step 8 — Assign RBAC for Model Access

Grant the container app's **system-assigned** identity the **Foundry User** role on the account that hosts the project:

```powershell
$sysId = az containerapp show --name bom-processor-gov --resource-group et-rg-adf `
  --query "identity.principalId" -o tsv

$cogScope = "/subscriptions/<subscription-id>/resourceGroups/et-rg-adf/providers/Microsoft.CognitiveServices/accounts/<project-hosting-account>"
az role assignment create --assignee $sysId --role "Foundry User" --scope $cogScope
```

> **Role note:** `Cognitive Services OpenAI User` is NOT sufficient for Foundry AIServices accounts — use **Foundry User**. Allow ~2-5 min for RBAC propagation in Gov.

## Gov Step 9 — Smoke Test

```powershell
$endpoint = "https://bom-processor-gov.<env-name>.usgovvirginia.azurecontainerapps.us/responses"
$body = '{"input":"Confirm you are the BOM processor agent and describe your canonical format.","store":false}'
Invoke-RestMethod -Uri $endpoint -Method POST -ContentType "application/json" -Body $body -TimeoutSec 180 | ConvertTo-Json -Depth 6
```

A successful response has `"status": "completed"` and non-empty `"output"`.

## Gov Gotchas (learned during this deployment)

1. **Hosted Agents unavailable in Gov** → use Container Apps.
2. **SDK hardcodes commercial token scope** — `azure-ai-projects` always requests `https://ai.azure.com/.default`, which Gov managed-identity endpoints reject with `invalid_scope`. `main.py` wraps the credential to translate the scope to `https://ai.azure.us/.default` when the project endpoint is a `.azure.us` host.
3. **Portal may create a second AIServices account** — deploy the model and assign RBAC on the account that actually hosts the project.
4. **Use `Foundry User`, not `Cognitive Services OpenAI User`** for model invocation on AIServices accounts.

---

# Part B — Commercial Azure Deployment (Foundry Hosted Agent)

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

### Azure Government (active)

| Setting | Value |
|---|---|
| Cloud | `AzureUSGovernment` |
| Subscription | `37d47e73-56ae-433b-a340-86eac088c78e` (Azure subscription ITAR) |
| Resource group | `et-rg-adf` (usgovvirginia) |
| Foundry account | `azuregovagents-resource` (usgovvirginia) |
| Foundry project | `AzureGovAgents` |
| ACR | `etContainerRegistry` (`etcontainerregistry.azurecr.us`) |
| Container App | `bom-processor-gov` in env `et-bom-ca-env` |
| Agent endpoint | `https://bom-processor-gov.calmglacier-732dba63.usgovvirginia.azurecontainerapps.us/responses` |
| Model | `gpt-5.6-luna` v2026-07-09, DataZoneStandard, capacity 10 (10,000 TPM) |
| Storage account | `etbomstoragegov` |
| Pull identity | `id-bom-processor-gov` (`4d332ea7-b039-423d-bcb4-97bd01c872d0`) |
| App system identity | `c4293fbe-7426-47be-84c4-c228777b51ea` |
| Image tag | `20260917b` |

### Commercial Azure (legacy)

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
