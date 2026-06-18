# Coding Assistant Agent — Deployment Setup Guide

End-to-end walkthrough for deploying the coding assistant as a Foundry Hosted Agent.
All commands are PowerShell (`pwsh`). Run them in order.

---

## Info You Need Before Starting

| Item | Where to get it | Example |
|---|---|---|
| Azure subscription ID | `az account show --query id -o tsv` | `be5a3beb-...` |
| Resource group name | Azure Portal or team lead | `ems-rg-e1` |
| Foundry account name | Chosen by you — must be globally unique | `et-foundry-e2` |
| ACR name | Chosen by you — must be globally unique, alphanumeric only | `etfoundryacr` |
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

## Step 3 — Build and Push the Container Image

Run from the repo root (where the `Dockerfile` lives). Uses ACR Cloud Build — no local Docker needed.

The image tag should be today's date (`yyyyMMdd`) to keep it unique and traceable.

```powershell
Set-Location "C:\path\to\aiFoundrySetup"

az acr build `
  --registry <acr-name> `
  --image coding-assistant:20260615 `
  --platform linux/amd64 `
  --file Dockerfile .
```

Verify the image landed:

```powershell
az acr repository show-tags --name <acr-name> --repository coding-assistant -o table
```

> **Note:** The Dockerfile uses `mcr.microsoft.com/devcontainers/python:3.12` as the base image to avoid Docker Hub rate limits during ACR cloud builds. Do not change it back to `python:3.12-slim`.

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
  "name": "coding-assistant",
  "definition": {
    "kind": "hosted",
    "image": "<acr-name>.azurecr.io/coding-assistant:<image-tag>",
    "cpu": "0.5",
    "memory": "1Gi",
    "container_protocol_versions": [
      { "protocol": "responses", "version": "1.0.0" }
    ],
    "environment_variables": {
      "AZURE_AI_MODEL_DEPLOYMENT_NAME_PRO": "DeepSeek-V4-Pro",
      "AZURE_AI_MODEL_DEPLOYMENT_NAME_FLASH": "DeepSeek-V4-Flash"
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
```

---

## Step 10 — Smoke Test

After RBAC is assigned (allow ~2 min for propagation):

```powershell
$token = az account get-access-token --resource "https://ai.azure.com" --query "accessToken" -o tsv
$endpoint = "https://<foundry-account-name>.services.ai.azure.com/api/projects/<project-name>/agents/coding-assistant/endpoint/protocols/openai/responses"

# Simple test message
$body = '{"input":"Say hello and confirm you are the coding assistant."}'
Invoke-RestMethod -Uri $endpoint -Method POST `
  -Headers @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" } `
  -Body $body | ConvertTo-Json -Depth 5
```

---

## Team Endpoint

Once deployed, the agent is available at:

```
https://<foundry-account-name>.services.ai.azure.com/api/projects/<project-name>/agents/coding-assistant/endpoint/protocols/openai/responses
```

Team members authenticate with `az login` + `DefaultAzureCredential` in code, or via the Foundry Portal at **[ai.azure.com](https://ai.azure.com)**.

---

## Redeployment (code changes)

To push a new version after editing `main.py`:

```powershell
# 1. Rebuild with a new date tag
az acr build `
  --registry <acr-name> `
  --image coding-assistant:<new-date-tag> `
  --platform linux/amd64 `
  --file Dockerfile .

# 2. Update agent-payload.json with the new image tag, then re-POST
az rest --method POST `
  --url "https://<foundry-account-name>.services.ai.azure.com/api/projects/<project-name>/agents?api-version=2025-05-15-preview" `
  --resource "https://ai.azure.com" `
  --body "@.foundry/agent-payload.json"
```

Each POST creates a new immutable version. Traffic routes to `@latest` automatically.

---

## Current Deployment Values (this repo)

| Setting | Value |
|---|---|
| Subscription | `be5a3beb-686a-4703-aaa8-ceb70667ad77` |
| Resource group | `ems-rg-e1` |
| Foundry account | `et-foundry-e2` (East US 2) |
| Foundry project | `CodingAssistant` |
| ACR | `etfoundryacr.azurecr.io` |
| Agent name | `coding-assistant` |
| Model | `DeepSeek-V4-Pro` GlobalStandard 200K TPM |
| Agent instance identity | `9a574021-0728-4f3c-bf9a-7966b97f1070` |
| Blueprint principal | `462a694e-5c1e-4864-a5e1-cb309f488e54` |
| Project managed identity | `59a291d9-4ad5-4fe9-9cae-a530bbbd7a86` |
