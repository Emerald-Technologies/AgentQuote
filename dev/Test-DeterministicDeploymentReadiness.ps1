<#
.SYNOPSIS
Validates local deterministic-service deployment artifacts without Azure calls.

.DESCRIPTION
Checks required files, rejects unresolved production-risk settings, verifies the
review template contains placeholders rather than secrets, and runs compilation
and tests. This script does not build, deploy, authenticate, or access Azure.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$repo = Split-Path -Parent $PSScriptRoot
Push-Location $repo
try {
    $required = @(
        "Dockerfile.deterministic",
        "deterministic_api.py",
        "bom_processor/core.py",
        ".foundry/deterministic-containerapp-gov.yaml"
    )
    foreach ($path in $required) {
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
            throw "Missing required deployment artifact: $path"
        }
    }

    $template = Get-Content -LiteralPath ".foundry/deterministic-containerapp-gov.yaml" -Raw
    $requiredMarkers = @(
        'external: false',
        'BOM_STORAGE_ACCOUNT_URL',
        'BOM_API_AUTH_MODE',
        'value: easy_auth',
        'BOM_API_ALLOWED_APP_IDS',
        '${QUICK_QUOTE_WORKLOAD_APP_ID}'
    )
    foreach ($marker in $requiredMarkers) {
        if (-not $template.Contains($marker)) {
            throw "Deployment template is missing required marker: $marker"
        }
    }
    $forbidden = @('BOM_STORAGE_CONNECTION_STRING', 'AccountKey=', 'SharedAccessSignature=')
    foreach ($marker in $forbidden) {
        if ($template.Contains($marker)) {
            throw "Deployment template contains forbidden secret configuration: $marker"
        }
    }

    python -m compileall -q bom_processor deterministic_api.py tests
    if ($LASTEXITCODE -ne 0) { throw "Python compilation failed." }
    python -m pytest -q
    if ($LASTEXITCODE -ne 0) { throw "Python tests failed." }

    Write-Host "Local deployment-readiness checks passed." -ForegroundColor Green
    Write-Warning "Azure Easy Auth, RBAC, ingress, and parser sandbox gates still require review before deployment."
}
finally {
    Pop-Location
}
