# Invoke-BomAgent.ps1
# Purpose: Submit a prompt to the BOM Processor hosted agent in background mode,
#          then poll until it completes and print the final output.
# Inputs:  -InputText (required) — the prompt to send to the agent.
#          -PollSeconds (optional, default 30) — wait between status checks.
#          -MaxMinutes (optional, default 20) — stop polling after this long.
# Outputs: Final agent response JSON printed to console.
# Usage:
#   .\dev\Invoke-BomAgent.ps1 -InputText "Take the files in boms/Astronics and build a parser script with input output positional args in parser/Astronics."
#   .\dev\Invoke-BomAgent.ps1 -InputText "..." -PollSeconds 20 -MaxMinutes 30

param(
    [Parameter(Mandatory = $true)]
    [string]$InputText,

    [int]$PollSeconds = 60,

    [int]$MaxMinutes = 25
)

$ErrorActionPreference = "Stop"

# --- Configuration ---
$FoundryAccount = "et-foundry-e2"
$Project        = "CodingAssistant"
$AgentName      = "bom-processor"
$ApiVersion     = "2025-05-15-preview"
$TenantId       = "c868cf0f-71da-4376-af70-e77141e1b38d"

$BaseUrl  = "https://$FoundryAccount.services.ai.azure.com/api/projects/$Project/agents/$AgentName/endpoint/protocols/openai/responses"
$Endpoint = "${BaseUrl}?api-version=$ApiVersion"

# --- 0. Ensure logged into Azure Commercial ---
Write-Host "Checking Azure login..." -ForegroundColor Cyan
az cloud set --name AzureCloud | Out-Null
$account = az account show 2>$null | ConvertFrom-Json
if (-not $account) {
    Write-Host "Not logged in. Opening browser for az login..." -ForegroundColor Yellow
    az login --tenant $TenantId | Out-Null
    $account = az account show | ConvertFrom-Json
    if (-not $account) {
        Write-Error "az login failed. Authenticate manually and re-run."
        exit 1
    }
}
Write-Host "Logged in as: $($account.user.name) (subscription: $($account.name))" -ForegroundColor Green

# --- 1. Get access token ---
Write-Host "Acquiring access token..." -ForegroundColor Cyan
$token = az account get-access-token --resource "https://ai.azure.com" --query "accessToken" -o tsv
if (-not $token) {
    Write-Error "Failed to acquire token. Run 'az login' first."
    exit 1
}
$headers = @{ Authorization = "Bearer $token"; "Content-Type" = "application/json" }

# --- 2. Submit in background mode ---
Write-Host "Submitting request (background mode)..." -ForegroundColor Cyan
$body = @{ input = $InputText; background = $true } | ConvertTo-Json

try {
    $resp = Invoke-RestMethod -Uri $Endpoint -Method POST -Headers $headers -Body $body
} catch {
    Write-Error "Submission failed: $($_.Exception.Message)"
    Write-Host $_.ErrorDetails.Message -ForegroundColor Red
    exit 1
}

$rid = $resp.id
if (-not $rid) {
    Write-Error "No response ID returned. Raw response:"
    $resp | ConvertTo-Json -Depth 5
    exit 1
}
Write-Host "Response ID: $rid" -ForegroundColor Green
Write-Host "Initial status: $($resp.status)" -ForegroundColor Green

# --- 3. Poll until completed or failed ---
$deadline = (Get-Date).AddMinutes($MaxMinutes)
$pollUrl  = "$BaseUrl/$($rid)?api-version=$ApiVersion"
$pollHeaders = @{ Authorization = "Bearer $token" }

Write-Host "Polling every $PollSeconds s (timeout: $MaxMinutes min)..." -ForegroundColor Cyan
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds $PollSeconds

    # Refresh token if close to expiry (tokens last ~1h; cheap to refresh each poll)
    $token = az account get-access-token --resource "https://ai.azure.com" --query "accessToken" -o tsv
    $pollHeaders = @{ Authorization = "Bearer $token" }

    try {
        $status_resp = Invoke-RestMethod -Uri $pollUrl -Method GET -Headers $pollHeaders
    } catch {
        Write-Warning "Poll failed (will retry): $($_.Exception.Message)"
        continue
    }

    $status = $status_resp.status
    $ts = Get-Date -Format "HH:mm:ss"
    Write-Host "[$ts] status: $status" -ForegroundColor Yellow

    if ($status -eq "completed") {
        Write-Host "`n=== FINAL OUTPUT ===" -ForegroundColor Green
        $status_resp | ConvertTo-Json -Depth 10
        exit 0
    }
    if ($status -in @("failed", "cancelled")) {
        Write-Host "`n=== REQUEST $status ===" -ForegroundColor Red
        $status_resp | ConvertTo-Json -Depth 10
        exit 1
    }
    # in_progress / queued -> keep polling
}

Write-Warning "Timed out after $MaxMinutes minutes. Check manually with:"
Write-Host "Invoke-RestMethod -Uri '$pollUrl' -Headers @{ Authorization = 'Bearer `$token' }"
