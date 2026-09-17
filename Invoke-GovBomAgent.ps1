# Invoke-GovBomAgent.ps1
# Purpose: Submit a prompt to the BOM Processor agent running in Azure Government
#          (Container App hosting) in background mode, then poll until it
#          completes and print the final output.
#
# Differences from the commercial Invoke-BomAgent.ps1:
#   - Targets the Container App endpoint in usgovvirginia (no Foundry hosted-agent URL).
#   - No authentication required (ingress is currently open).
#   - Background mode is enabled via resilient tasks (TaskManager with
#     LocalFileTaskProvider) on the agent side.
#
# Inputs:  -InputText (required) — the prompt to send to the agent.
#          -PollSeconds (optional, default 60) — wait between status checks.
#          -MaxMinutes (optional, default 25) — stop polling after this long.
# Outputs: Final agent response JSON printed to console.
# Usage:
#   .\Invoke-GovBomAgent.ps1 -InputText "Take the files in boms/Astronics and build a parser script with input output positional args in parser/Astronics."
#   .\Invoke-GovBomAgent.ps1 -InputText "..." -PollSeconds 20 -MaxMinutes 30

param(
    [Parameter(Mandatory = $true)]
    [string]$InputText,

    [int]$PollSeconds = 60,

    [int]$MaxMinutes = 25
)

$ErrorActionPreference = "Stop"

# --- Configuration ---
$BaseUrl = "https://bom-processor-gov.calmglacier-732dba63.usgovvirginia.azurecontainerapps.us/responses"

# --- 1. Submit in background mode (no auth required — open ingress) ---
Write-Host "Submitting request (background mode)..." -ForegroundColor Cyan
Write-Host "Endpoint: $BaseUrl" -ForegroundColor DarkGray

$body = @{ input = $InputText; background = $true; store = $true } | ConvertTo-Json

try {
    $resp = Invoke-RestMethod -Uri $BaseUrl -Method POST -ContentType "application/json" -Body $body -TimeoutSec 120
} catch {
    Write-Error "Submission failed: $($_.Exception.Message)"
    if ($_.ErrorDetails.Message) { Write-Host $_.ErrorDetails.Message -ForegroundColor Red }
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

# --- 2. Poll until completed or failed ---
$deadline = (Get-Date).AddMinutes($MaxMinutes)
$pollUrl  = "$BaseUrl/$rid"

Write-Host "Polling every $PollSeconds s (timeout: $MaxMinutes min)..." -ForegroundColor Cyan
while ((Get-Date) -lt $deadline) {
    Start-Sleep -Seconds $PollSeconds

    try {
        $status_resp = Invoke-RestMethod -Uri $pollUrl -Method GET -TimeoutSec 60
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
Write-Host "Invoke-RestMethod -Uri '$pollUrl'"
