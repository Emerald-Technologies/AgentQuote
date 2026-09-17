# Azure Government Agent Deployment Guide

A field-tested playbook for building and deploying Agent Framework agents in
Azure Government, based on the bom-processor deployment (Sept 2026).

Copy this file into your new workspace and follow it top-to-bottom. Every
step reflects a real gotcha we hit — the warnings are not theoretical.

---

## TL;DR — The 5 Gotchas (read these first)

1. **Hosted Agents do NOT exist in Azure Gov.**
   `POST .../agents` returns `Hosted Agents are not enabled in this region`.
   ➜ Host the agent on **Azure Container Apps** instead. The container image
   is identical — only the hosting changes.

2. **The SDK hardcodes the commercial token scope.**
   `azure-ai-projects` always requests `https://ai.azure.com/.default`, even
   when you pass a `.azure.us` endpoint. Gov managed-identity endpoints reject
   that audience with `invalid_scope`.
   ➜ Wrap the credential to translate the scope (code in Step 8).

3. **Use the `Foundry User` role, not `Cognitive Services OpenAI User`.**
   The legacy role is not sufficient for AIServices accounts — model calls
   return 403. Assign `Foundry User` on the Cognitive Services account that
   hosts your project.

4. **The portal may create a second AIServices account.**
   When you create the project in ai.azure.us, the portal can create its own
   account instead of using the one you provisioned. The model deployment and
   RBAC must target the account that actually hosts the project.
   ➜ Check `az cognitiveservices account list` after creating the project.

5. **Model versions must be exact.**
   Deployments fail with `DeploymentModelNotSupported` if the version string
   is wrong (e.g. using the model name as the version). Look up the exact
   version first (Step 4).

Other things worth knowing:
- All Gov endpoints use `.us` suffixes: `management.usgovcloudapi.net`,
  `*.services.ai.azure.us`, `*.azurecr.us`, `core.usgovcloudapi.net` (storage).
- RBAC propagation in Gov can take 2–5 minutes. Don't panic on a 403 right
  after assigning a role.
- `az rest` calls need `--resource "https://management.usgovcloudapi.net"`
  (the CLI can't auto-derive the Gov ARM audience from the URL).

---

## Prerequisites

| Item | Notes |
|---|---|
| Azure CLI logged into Gov | `az cloud set --name AzureUSGovernment; az login` |
| Resource group | Must already exist in a Gov region (we used `usgovvirginia`) |
| Owner/UAA on the RG | Needed for role assignments |
| Container Registry | Reuse an existing Gov ACR or create one |
| Model quota | Check with `az cognitiveservices usage list --location <region>` |

Verify your environment:

```powershell
az cloud show --query name -o tsv                    # must be AzureUSGovernment
az account show --query "{name:name, id:id}" -o table
az group show --name <resource-group> --query location -o tsv
```

---

## Step 1 — Check model availability and quota

```powershell
az cognitiveservices usage list --location <region> `
  --query "[?contains(name.value,'<model-name>')].{name:name.value, current:currentValue, limit:limit}" -o table
```

Example output for gpt-5.6-luna in usgovvirginia:
`OpenAI.DataZoneStandard.gpt-5.6-luna  0.0  1000.0`

The SKU prefix matters — it tells you which SKU to deploy with
(`DataZoneStandard` here). Capacity units on DataZoneStandard are in
increments of 10K TPM (capacity 10 = 10,000 TPM).

---

## Step 2 — Create the AIServices (Foundry) resource

Create `.foundry/account-create-gov.json`:

```json
{
  "kind": "AIServices",
  "sku": { "name": "S0" },
  "location": "<region>",
  "identity": { "type": "SystemAssigned" },
  "properties": {
    "customSubDomainName": "<account-name>",
    "publicNetworkAccess": "Enabled"
  }
}
```

```powershell
az rest --method PUT `
  --url "https://management.usgovcloudapi.net/subscriptions/<sub-id>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<account-name>?api-version=2024-10-01" `
  --resource "https://management.usgovcloudapi.net" `
  --body "@.foundry/account-create-gov.json" `
  --query "{name:name, state:properties.provisioningState, identity:identity.type}" -o table
```

Expected: `Succeeded` / `SystemAssigned`.

> Identity + custom subdomain MUST be set in this single call. Adding them
> later breaks project creation.
> If you get `FlagMustBeSetForRestore`, the name was soft-deleted before —
> add `"restore": true` inside `properties` and rerun.

---

## Step 3 — Create the Foundry project (portal)

1. Go to **ai.azure.us** (NOT ai.azure.com)
2. **+ Create project** → select your AIServices resource → name it
3. Copy the project endpoint:
   `https://<account>.services.ai.azure.us/api/projects/<project>`

⚠️ **Immediately after, check for the duplicate-account gotcha:**

```powershell
az cognitiveservices account list -g <rg> --query "[].{name:name, subdomain:properties.customSubDomainName}" -o table
```

If the portal created a *different* account than the one you provisioned,
**use the portal's account for everything from here on** (model deployment,
RBAC). This is what happened to us: project `AzureGovAgents` landed on
`azuregovagents-resource`, not on the account we created.

---

## Step 4 — Find the exact model version, then deploy

Get the version (do NOT guess — using the model name as the version fails):

```powershell
az cognitiveservices account list-models -n <project-hosting-account> -g <rg> -o table
```

Deploy (JSON body avoids PowerShell quoting issues):

`.foundry/model-deployment-gov.json`:

```json
{
  "sku": { "name": "DataZoneStandard", "capacity": 10 },
  "properties": {
    "model": {
      "format": "OpenAI",
      "name": "<model-name>",
      "version": "<exact-version>"
    }
  }
}
```

```powershell
az rest --method PUT `
  --url "https://management.usgovcloudapi.net/subscriptions/<sub-id>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<project-hosting-account>/deployments/<deployment-name>?api-version=2024-10-01" `
  --resource "https://management.usgovcloudapi.net" `
  --body "@.foundry/model-deployment-gov.json" `
  --query "{name:name, capacity:sku.capacity, state:properties.provisioningState}" -o table
```

Verify the model works with YOUR identity before wiring up the agent:

```powershell
$tok = az account get-access-token --resource "https://ai.azure.us" --query accessToken -o tsv
$url = "https://<account>.services.ai.azure.us/api/projects/<project>/openai/v1/responses"
Invoke-RestMethod -Uri $url -Method POST -ContentType "application/json" `
  -Headers @{Authorization="Bearer $tok"} `
  -Body '{"model":"<deployment-name>","input":"Say hi"}' -TimeoutSec 60 | ConvertTo-Json -Depth 3
```

If this returns `DeploymentNotFound`, you deployed the model on the wrong
account (gotcha #4).

---

## Step 5 — Storage account (if your agent needs blob storage)

```powershell
az storage account create --name <storage-name> --resource-group <rg> `
  --location <region> --sku Standard_LRS

foreach ($c in '<container1>','<container2>') {
  az storage container create --name $c --account-name <storage-name> --auth-mode login
}

# The FULL connection string goes into the agent's env var — don't pick
# individual pieces (DefaultEndpointsProtocol, BlobEndpoint, etc.) out of it.
az storage account show-connection-string --name <storage-name> `
  --resource-group <rg> --query connectionString -o tsv
```

Gov storage endpoints use `core.usgovcloudapi.net` — the connection string
includes that automatically.

---

## Step 6 — Build and push the container image

```powershell
az acr build --registry <acr-name> --image <agent-name>:<tag> `
  --platform linux/amd64 --file Dockerfile .
```

Dockerfile notes from the bom-processor build:
- `python:3.12-slim` base works fine in Gov ACR builds
- Set an explicit `ENTRYPOINT` — base images with shell-init entrypoints hang
- The server must honor the `PORT` env var or default to a known port
  (agentserver defaults to 8088)

> Known CLI annoyance: `az acr build` may crash locally with a
> `UnicodeEncodeError` while streaming logs. The build usually still runs
> server-side — check `az acr task list-runs --registry <acr-name>` and
> `az acr repository show-tags` before retrying.

---

## Step 7 — Deploy to Container Apps

```powershell
# Pull identity for ACR access (no admin keys needed)
az identity create --name id-<agent-name>-gov --resource-group <rg> --location <region>

$acrScope = "/subscriptions/<sub-id>/resourceGroups/<rg>/providers/Microsoft.ContainerRegistry/registries/<acr-name>"
az role assignment create --assignee "<pull-identity-principal-id>" --role "AcrPull" --scope $acrScope

# Environment (reuse an existing one if available)
az containerapp env create --name <env-name> --resource-group <rg> --location <region>
```

App YAML (`.foundry/<agent-name>-containerapp-gov.yaml`):

```yaml
location: <region>
identity:
  type: UserAssigned,SystemAssigned
  userAssignedIdentities:
    "/subscriptions/<sub-id>/resourceGroups/<rg>/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-<agent-name>-gov": {}
properties:
  managedEnvironmentId: "/subscriptions/<sub-id>/resourceGroups/<rg>/providers/Microsoft.App/managedEnvironments/<env-name>"
  configuration:
    secrets:
      - name: my-secret
        value: "<secret-value>"
    registries:
      - server: <acr-name>.azurecr.us
        identity: "/subscriptions/<sub-id>/resourceGroups/<rg>/providers/Microsoft.ManagedIdentity/userAssignedIdentities/id-<agent-name>-gov"
    ingress:
      external: true
      targetPort: 8088
      transport: http
      allowInsecure: false
  template:
    containers:
      - name: <agent-name>
        image: <acr-name>.azurecr.us/<agent-name>:<tag>
        env:
          - name: FOUNDRY_PROJECT_ENDPOINT
            value: "https://<account>.services.ai.azure.us/api/projects/<project>"
          - name: AZURE_AI_MODEL_DEPLOYMENT_NAME_FLASH
            value: "<deployment-name>"
          - name: MY_SECRET
            secretRef: my-secret
        resources:
          cpu: 0.5
          memory: 1Gi
    scale:
      minReplicas: 1
      maxReplicas: 2
```

```powershell
az containerapp create --name <agent-name>-gov --resource-group <rg> `
  --yaml .foundry/<agent-name>-containerapp-gov.yaml
```

Key points:
- **Two identities**: UserAssigned (pulls the image) + SystemAssigned (calls the model)
- Secrets (connection strings, keys) go in `configuration.secrets` with
  `secretRef` — never as plain env values
- The first startup probe may log `ProbeFailed` — that's normal if the probe
  fires before the server finishes booting. Check console logs, not events.

---

## Step 8 — The Gov scope fix (required in agent code)

The `azure-ai-projects` SDK hardcodes the token scope to
`https://ai.azure.com/.default` inside `AIProjectClient._get_openai_api_key()`.
This is true as of v2.6.1. The `credential_scopes` kwarg only affects ARM
management calls, not the OpenAI inference path.

Without the fix, the first model call fails with:
`ManagedIdentityCredential: ... Token request error: (invalid_scope) 400`

Add this wrapper around your credential (from bom-processor's `main.py`):

```python
credential = DefaultAzureCredential()  # or ManagedIdentityCredential(...)

# Azure Gov fix: translate the hardcoded commercial scope to the Gov audience.
_project_ep = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
if ".azure.us" in _project_ep:
    class _GovScopeCredential:
        """Translate commercial AI scope to Gov audience."""
        def __init__(self, inner):
            self._inner = inner
        def get_token(self, *scopes, **kwargs):
            translated = tuple(
                s.replace("https://ai.azure.com/.default",
                          "https://ai.azure.us/.default")
                for s in scopes
            )
            return self._inner.get_token(*translated, **kwargs)
    credential = _GovScopeCredential(credential)

client = FoundryChatClient(
    project_endpoint=_project_ep,
    model=os.environ["AZURE_AI_MODEL_DEPLOYMENT_NAME_FLASH"],
    credential=credential,
)
```

The same fix applies to **any** Gov service that calls Foundry models with a
managed identity — e.g. a Function App using
`ManagedIdentityCredential(client_id="<user-assigned-MI-client-id>")`.

---

## Step 9 — RBAC: grant the agent model access

```powershell
$sysId = az containerapp show --name <agent-name>-gov --resource-group <rg> `
  --query "identity.principalId" -o tsv

$cogScope = "/subscriptions/<sub-id>/resourceGroups/<rg>/providers/Microsoft.CognitiveServices/accounts/<project-hosting-account>"
az role assignment create --assignee $sysId --role "Foundry User" --scope $cogScope
```

Rules learned the hard way:
- Use **`Foundry User`** — `Cognitive Services OpenAI User` returns 403 on
  AIServices accounts
- Scope it to the account that **hosts the project** (gotcha #4)
- Wait 2–5 minutes for propagation before smoke testing

Granting other callers (function apps, team members) the same pattern:

```powershell
az role assignment create --assignee "<object-id>" --role "Foundry User" --scope $cogScope
```

---

## Step 10 — Enable background mode (optional but recommended)

The agentserver host only initializes the TaskManager when explicitly enabled.
Without it, `background: true` requests degrade and long-running jobs must be
called synchronously.

In `main.py`, before `server.run()`:

```python
from azure.ai.agentserver.core.tasks import set_resilient_tasks_enabled
set_resilient_tasks_enabled(True)
```

In non-hosted mode (which Container Apps is), tasks persist via
`LocalFileTaskProvider` under `~/.agentserver/tasks` — no external
dependencies. Caveat: task state lives on the container filesystem, so a
rescheduled replica loses in-flight tasks. Fine for single-replica workloads;
add persistent storage if you need cross-node durability.

Confirm in logs after deploy:
`TaskManager initialized with startup recovery`

---

## Step 11 — Smoke tests

**Sync test:**

```powershell
$endpoint = "https://<agent-name>-gov.<env-suffix>.<region>.azurecontainerapps.us/responses"
Invoke-RestMethod -Uri $endpoint -Method POST -ContentType "application/json" `
  -Body '{"input":"Confirm you are online.","store":false}' -TimeoutSec 180 | ConvertTo-Json -Depth 6
```

**Background test:**

```powershell
$resp = Invoke-RestMethod -Uri $endpoint -Method POST -ContentType "application/json" `
  -Body '{"input":"Confirm you are online.","background":true,"store":true}' -TimeoutSec 60
Write-Host "ID: $($resp.id)  Status: $($resp.status)"   # expect in_progress
Start-Sleep 30
Invoke-RestMethod -Uri "$endpoint/$($resp.id)" -Method GET | ConvertTo-Json -Depth 6
```

---

## Redeployment (code changes)

```powershell
az acr build --registry <acr-name> --image <agent-name>:<new-tag> --platform linux/amd64 --file Dockerfile .
az containerapp update --name <agent-name>-gov --resource-group <rg> `
  --image <acr-name>.azurecr.us/<agent-name>:<new-tag>
```

Tag convention: `yyyyMMdd`, append letters for same-day rebuilds (`20260917b`).

---

## Troubleshooting table

| Symptom | Cause | Fix |
|---|---|---|
| `Hosted Agents are not enabled in this region` | Hosted Agents unavailable in Gov | Use Container Apps (this guide) |
| `invalid_scope` / 400 from ManagedIdentityCredential | SDK hardcoded commercial scope | Step 8 credential wrapper |
| 403 on model call | Wrong role or wrong account scope | `Foundry User` on project-hosting account |
| `DeploymentNotFound` | Model deployed on wrong account | Deploy on the account hosting the project |
| `DeploymentModelNotSupported` | Wrong model version string | Look up exact version via `list-models` |
| `FlagMustBeSetForRestore` | Soft-deleted account name | Add `"restore": true` to properties |
| `ProbeFailed` at startup | Probe fires before server boots | Check console logs; usually benign |
| `az acr build` UnicodeEncodeError | CLI display bug | Check `az acr task list-runs`; build likely succeeded |
| `az rest` SubscriptionNotFound | Used commercial ARM host | Use `management.usgovcloudapi.net` + `--resource` |
| `] was unexpected at this time` | JMESPACE query mangled by cmd | Quote queries or run in pwsh, not cmd |

---

## bom-processor reference values (working example)

| Setting | Value |
|---|---|
| Subscription | `37d47e73-56ae-433b-a340-86eac088c78e` (ITAR) |
| Resource group | `et-rg-adf` (usgovvirginia) |
| Project-hosting account | `azuregovagents-resource` |
| Project | `AzureGovAgents` |
| Model | `gpt-5.6-luna` v2026-07-09, DataZoneStandard, capacity 10 |
| ACR | `etContainerRegistry` (`etcontainerregistry.azurecr.us`) |
| Container App | `bom-processor-gov` in `et-bom-ca-env` |
| Endpoint | `https://bom-processor-gov.calmglacier-732dba63.usgovvirginia.azurecontainerapps.us/responses` |
| Storage | `etbomstoragegov` |

Source repo: `Emerald-Technologies/aiFoundrySetup` (agentQuote folder) — see
`setup.md` Part A for the original walkthrough and `main.py` for the scope fix.
