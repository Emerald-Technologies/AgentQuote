<#
.SYNOPSIS
    Deploys a Foundry Hosted Agent from scratch.

.DESCRIPTION
    Provisions ACR, builds/pushes the container image, creates an AIServices resource
    with managed identity, deploys a model, and creates the hosted agent.
    Outputs the principal IDs needed for the RBAC step (requires Owner/UAA to complete).

.PARAMETER SubscriptionId
    Azure subscription ID.

.PARAMETER ResourceGroup
    Resource group name (must already exist).

.PARAMETER Region
    Azure region. Must support Foundry Hosted Agents (e.g. eastus2, northcentralus).
    Full list: https://learn.microsoft.com/azure/foundry/agents/concepts/hosted-agents#region-availability

.PARAMETER FoundryAccountName
    Name for the new AIServices resource. Must be globally unique. Used as the custom subdomain.

.PARAMETER ProjectName
    Name for the Foundry project. Created via portal after this script runs (see output).

.PARAMETER AcrName
    Name for the Azure Container Registry. Must be globally unique, alphanumeric only.

.PARAMETER ImageTag
    Tag for the container image (e.g. 20260615). Use today's date for traceability.

.PARAMETER ModelName
    Model deployment name (default: DeepSeek-V4-Pro).

.PARAMETER ModelVersion
    Model version string (default: 2026-04-23).

.PARAMETER ModelFormat
    Model format/publisher (default: DeepSeek).

.PARAMETER ModelCapacity
    GlobalStandard TPM capacity in thousands (default: 200).

.PARAMETER AgentName
    Name for the hosted agent (default: coding-assistant).

.PARAMETER DockerfilePath
    Path to the Dockerfile directory. Defaults to current directory.

.EXAMPLE
    .\deploy-agent.ps1 `
        -SubscriptionId "be5a3beb-686a-4703-aaa8-ceb70667ad77" `
        -ResourceGroup "ems-rg-e1" `
        -Region "eastus2" `
        -FoundryAccountName "my-foundry" `
        -ProjectName "MyProject" `
        -AcrName "myfoundryacr" `
        -ImageTag "20260615"
#>

[CmdletBinding()]
param(
    [Parameter(Mandatory)][string] $SubscriptionId,
    [Parameter(Mandatory)][string] $ResourceGroup,
    [Parameter(Mandatory)][string] $Region,
    [Parameter(Mandatory)][string] $FoundryAccountName,
    [Parameter(Mandatory)][string] $ProjectName,
    [Parameter(Mandatory)][string] $AcrName,
    [Parameter(Mandatory)][string] $ImageTag,

    [string] $ModelName     = "DeepSeek-V4-Pro",
    [string] $ModelVersion  = "2026-04-23",
    [string] $ModelFormat   = "DeepSeek",
    [int]    $ModelCapacity = 200,
    [string] $AgentName     = "coding-assistant",
    [string] $DockerfilePath = "."
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$cogScope = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.CognitiveServices/accounts/$FoundryAccountName"
$acrScope = "/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.ContainerRegistry/registries/$AcrName"
$foundryApi = "https://$FoundryAccountName.services.ai.azure.com/api/projects/$ProjectName"
$payloadDir = Join-Path $PSScriptRoot ".foundry"

function Write-Step([string]$msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-Ok([string]$msg)   { Write-Host "  OK: $msg"    -ForegroundColor Green }
function Write-Warn([string]$msg) { Write-Host "  !! $msg"     -ForegroundColor Yellow }

# ---------------------------------------------------------------------------
Write-Step "1. Verify subscription and login"
az account set --subscription $SubscriptionId
$sub = az account show --query "name" -o tsv
Write-Ok "Active subscription: $sub"

# ---------------------------------------------------------------------------
Write-Step "2. Create Azure Container Registry"
$existing = az acr show --name $AcrName --resource-group $ResourceGroup --query "name" -o tsv 2>$null
if ($existing) {
    Write-Warn "ACR '$AcrName' already exists — skipping creation"
} else {
    az acr create `
        --name $AcrName `
        --resource-group $ResourceGroup `
        --sku Basic `
        --location $Region `
        --allow-trusted-services true `
        --query "{name:name, loginServer:loginServer}" -o table
    Write-Ok "ACR created"
}

# ---------------------------------------------------------------------------
Write-Step "3. Build and push container image via ACR Cloud Build"
Push-Location $DockerfilePath
try {
    az acr build `
        --registry $AcrName `
        --image "${AgentName}:${ImageTag}" `
        --platform linux/amd64 `
        --file Dockerfile .
    Write-Ok "Image ${AgentName}:${ImageTag} pushed to $AcrName"
} finally {
    Pop-Location
}

# ---------------------------------------------------------------------------
Write-Step "4. Create Foundry AIServices resource with managed identity"
$accountExists = az cognitiveservices account show --name $FoundryAccountName --resource-group $ResourceGroup --query "name" -o tsv 2>$null
if ($accountExists) {
    Write-Warn "Account '$FoundryAccountName' already exists — skipping creation"
} else {
    $accountPayload = @{
        kind       = "AIServices"
        sku        = @{ name = "S0" }
        location   = $Region
        identity   = @{ type = "SystemAssigned" }
        properties = @{
            customSubDomainName = $FoundryAccountName
            publicNetworkAccess = "Enabled"
        }
    } | ConvertTo-Json -Depth 5

    # Write to temp file — inline JSON causes quoting issues in PowerShell
    $tmpFile = Join-Path $env:TEMP "foundry-account-create.json"
    $accountPayload | Set-Content $tmpFile -Encoding UTF8

    az rest --method PUT `
        --url "https://management.azure.com/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.CognitiveServices/accounts/${FoundryAccountName}?api-version=2024-10-01" `
        --body "@$tmpFile" `
        --query "{name:name, location:location, state:properties.provisioningState, identity:identity.type}" -o table

    Remove-Item $tmpFile
    Write-Ok "Foundry account created in $Region"
}

# ---------------------------------------------------------------------------
Write-Step "5. Deploy model $ModelName"
$deployExists = az cognitiveservices account deployment show `
    --name $FoundryAccountName `
    --resource-group $ResourceGroup `
    --deployment-name $ModelName `
    --query "name" -o tsv 2>$null
if ($deployExists) {
    Write-Warn "Deployment '$ModelName' already exists — skipping"
} else {
    az cognitiveservices account deployment create `
        --name $FoundryAccountName `
        --resource-group $ResourceGroup `
        --deployment-name $ModelName `
        --model-name $ModelName `
        --model-version $ModelVersion `
        --model-format $ModelFormat `
        --sku-name GlobalStandard `
        --sku-capacity $ModelCapacity `
        --query "{name:name, model:properties.model.name, state:properties.provisioningState}" -o table
    Write-Ok "Model '$ModelName' deployed"
}

# ---------------------------------------------------------------------------
Write-Step "6. Verify Foundry project endpoint (portal step)"
Write-Warn "Project creation requires the portal — this script cannot create it automatically."
Write-Host ""
Write-Host "  If you haven't created the project yet:"
Write-Host "  1. Go to https://ai.azure.com"
Write-Host "  2. Click '+ Create project'"
Write-Host "  3. Select resource '$FoundryAccountName'"
Write-Host "  4. Name it '$ProjectName' -> Create"
Write-Host ""

$projectReady = $false
$attempts = 0
while (-not $projectReady -and $attempts -lt 3) {
    $attempts++
    $result = az rest --method GET `
        --url "$foundryApi/agents?api-version=2025-05-15-preview" `
        --resource "https://ai.azure.com" 2>&1
    if ($result -match '"object"') {
        $projectReady = $true
        Write-Ok "Project '$ProjectName' is reachable"
    } else {
        Write-Warn "Project not reachable yet (attempt $attempts/3). Create it in the portal, then press Enter to retry..."
        Read-Host
    }
}
if (-not $projectReady) {
    Write-Host "Could not reach project endpoint after 3 attempts. Complete the portal step and rerun from Step 7." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------------------
Write-Step "7. Create hosted agent '$AgentName'"

# Ensure payload dir exists
if (-not (Test-Path $payloadDir)) { New-Item -ItemType Directory $payloadDir | Out-Null }

$agentPayload = @{
    name       = $AgentName
    definition = @{
        kind       = "hosted"
        image      = "$AcrName.azurecr.io/${AgentName}:${ImageTag}"
        cpu        = "0.5"
        memory     = "1Gi"
        container_protocol_versions = @(@{ protocol = "responses"; version = "1.0.0" })
        environment_variables = @{
            AZURE_AI_MODEL_DEPLOYMENT_NAME = $ModelName
        }
    }
} | ConvertTo-Json -Depth 6

$agentPayloadFile = Join-Path $payloadDir "agent-payload.json"
$agentPayload | Set-Content $agentPayloadFile -Encoding UTF8

$agentResult = az rest --method POST `
    --url "$foundryApi/agents?api-version=2025-05-15-preview" `
    --resource "https://ai.azure.com" `
    --body "@$agentPayloadFile" | ConvertFrom-Json

$instancePrincipal  = $agentResult.instance_identity.principal_id
$blueprintPrincipal = $agentResult.blueprint.principal_id
Write-Ok "Agent '$AgentName' created (version 1)"

# ---------------------------------------------------------------------------
Write-Step "8. Get project managed identity (for AcrPull)"
$projectPrincipal = az rest --method GET `
    --url "https://management.azure.com/subscriptions/$SubscriptionId/resourceGroups/$ResourceGroup/providers/Microsoft.CognitiveServices/accounts/$FoundryAccountName/projects/${ProjectName}?api-version=2025-04-01-preview" `
    --query "identity.principalId" -o tsv

# ---------------------------------------------------------------------------
Write-Step "9. Update .env"
$envFile = Join-Path $PSScriptRoot ".env"
if (Test-Path $envFile) {
    $envContent = "FOUNDRY_PROJECT_ENDPOINT=`"$foundryApi`"`nAZURE_AI_MODEL_DEPLOYMENT_NAME=`"$ModelName`""
    $envContent | Set-Content $envFile -Encoding UTF8
    Write-Ok ".env updated with new project endpoint"
} else {
    Write-Warn ".env not found at $envFile — skipping update"
}

# ---------------------------------------------------------------------------
Write-Step "DONE — RBAC assignments required (needs Owner or User Access Administrator)"
Write-Host ""
Write-Host "Run these 3 commands as an Azure admin:" -ForegroundColor Yellow
Write-Host ""
Write-Host "# 1. Agent runtime -> Foundry User (allows agent to call the model)"
Write-Host "az role assignment create --assignee `"$instancePrincipal`" --role `"Foundry User`" --scope `"$cogScope`""
Write-Host ""
Write-Host "# 2. Blueprint principal -> Foundry User (platform infrastructure)"
Write-Host "az role assignment create --assignee `"$blueprintPrincipal`" --role `"Foundry User`" --scope `"$cogScope`""
Write-Host ""
Write-Host "# 3. Project identity -> AcrPull (allows Foundry to pull the container image)"
Write-Host "az role assignment create --assignee `"$projectPrincipal`" --role `"AcrPull`" --scope `"$acrScope`""
Write-Host ""
Write-Host "# 4. Per team member (repeat for each person who needs access)"
Write-Host "az role assignment create --assignee `"<email-or-object-id>`" --role `"Foundry User`" --scope `"$cogScope`""
Write-Host ""
Write-Host "Agent endpoint (available after RBAC is assigned):" -ForegroundColor Cyan
Write-Host "  $foundryApi/agents/$AgentName/endpoint/protocols/openai/responses"
Write-Host ""
Write-Host "Current deployment values:" -ForegroundColor Cyan
Write-Host "  Subscription:        $SubscriptionId"
Write-Host "  Resource group:      $ResourceGroup"
Write-Host "  Foundry account:     $FoundryAccountName ($Region)"
Write-Host "  Project:             $ProjectName"
Write-Host "  ACR:                 $AcrName.azurecr.io"
Write-Host "  Agent:               $AgentName"
Write-Host "  Model:               $ModelName GlobalStandard ${ModelCapacity}K TPM"
Write-Host "  Instance principal:  $instancePrincipal"
Write-Host "  Blueprint principal: $blueprintPrincipal"
Write-Host "  Project principal:   $projectPrincipal"
