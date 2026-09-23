<#
.SYNOPSIS
Creates the AgentQuote deterministic API registration and authorizes Quick Quote.

.DESCRIPTION
Azure Government / Microsoft Graph setup for server-to-server Easy Auth:
- Creates or reuses "AgentQuote Deterministic API - Gov".
- Adds the application role "bom.process".
- Creates or reuses the API service principal.
- Assigns bom.process to the existing Quick Quote managed identity.
- Verifies all resulting identifiers and the role assignment.

This script creates no passwords, client secrets, deployments, or customer data.
It is safe to rerun. Run only as an approved Entra administrator.

USAGE
  .\dev\Set-AgentQuoteEntraApi.ps1
#>
[CmdletBinding()]
param(
    [string]$SubscriptionId = "37d47e73-56ae-433b-a340-86eac088c78e",
    [string]$DisplayName = "AgentQuote Deterministic API - Gov",
    [string]$AppRoleValue = "bom.process",
    [string]$QuickQuoteClientId = "d853e6a6-5fa4-4182-9472-348edef96d52",
    [string]$QuickQuotePrincipalId = "073cf1d2-fa00-450b-9ea7-b6f29dd7fe2b",
    [switch]$SkipLogin
)

$ErrorActionPreference = "Stop"

function Assert-AzSuccess {
    param([Parameter(Mandatory = $true)][string]$Step)

    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed with Azure CLI exit code $LASTEXITCODE."
    }
}

$graphResource = "https://graph.microsoft.us"
$graphBase = "https://graph.microsoft.us/v1.0"
$tempRoot = [System.IO.Path]::GetTempPath()
if ([string]::IsNullOrWhiteSpace($tempRoot) -or -not [System.IO.Path]::IsPathRooted($tempRoot)) {
    throw "Windows returned an invalid temporary-directory path."
}
$tempDirectory = Join-Path $tempRoot ("agentquote-entra-setup-" + [guid]::NewGuid().Guid)
New-Item -ItemType Directory -Path $tempDirectory -Force | Out-Null

try {
    az cloud set --name AzureUSGovernment
    Assert-AzSuccess "Selecting Azure Government"

    if (-not $SkipLogin) {
        az login
        Assert-AzSuccess "Azure sign-in"
    }

    az account set --subscription $SubscriptionId
    Assert-AzSuccess "Selecting subscription"

    $account = az account show --query "{subscription:id,tenantId:tenantId,user:user.name}" --output json | ConvertFrom-Json
    Assert-AzSuccess "Reading Azure account"
    Write-Host "Using tenant $($account.tenantId), subscription $($account.subscription), user $($account.user)." -ForegroundColor Cyan

    $applications = @(
        az ad app list `
            --display-name $DisplayName `
            --query "[].{id:id,appId:appId,displayName:displayName}" `
            --output json |
        ConvertFrom-Json
    )
    Assert-AzSuccess "Looking up API application"

    if ($applications.Count -gt 1) {
        throw "Multiple application registrations named '$DisplayName' exist. Resolve the duplicate registrations before continuing."
    }

    if ($applications.Count -eq 0) {
        $createAppPath = Join-Path $tempDirectory "create-app.json"
        @{
            displayName = $DisplayName
            signInAudience = "AzureADMyOrg"
        } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $createAppPath -Encoding UTF8

        $apiApp = az rest `
            --method POST `
            --url "$graphBase/applications" `
            --resource $graphResource `
            --headers "Content-Type=application/json" `
            --body "@$createAppPath" `
            --output json |
        ConvertFrom-Json
        Assert-AzSuccess "Creating API application"
        Write-Host "Created API application registration." -ForegroundColor Green
    }
    else {
        $apiApp = az rest `
            --method GET `
            --url "$graphBase/applications/$($applications[0].id)" `
            --resource $graphResource `
            --output json |
        ConvertFrom-Json
        Assert-AzSuccess "Reading existing API application"
        Write-Host "API application registration already exists." -ForegroundColor Yellow
    }

    $apiAppId = [string]$apiApp.appId
    $apiAppObjectId = [string]$apiApp.id
    if ([string]::IsNullOrWhiteSpace($apiAppId) -or [string]::IsNullOrWhiteSpace($apiAppObjectId)) {
        throw "The API application returned an empty application ID or object ID."
    }
    $identifierUri = "api://$apiAppId"

    $apiApp = az rest `
        --method GET `
        --url "$graphBase/applications/$apiAppObjectId" `
        --resource $graphResource `
        --output json |
    ConvertFrom-Json
    Assert-AzSuccess "Reading API application configuration"

    $existingRole = @($apiApp.appRoles | Where-Object { $_.value -eq $AppRoleValue }) | Select-Object -First 1
    if ($existingRole) {
        $appRoleId = [string]$existingRole.id
        Write-Host "Application role '$AppRoleValue' already exists." -ForegroundColor Yellow
    }
    else {
        $appRoleId = [guid]::NewGuid().Guid
        $newRole = @{
            id = $appRoleId
            allowedMemberTypes = @("Application")
            description = "Allows Quick Quote to submit deterministic BOM processing requests."
            displayName = "Process BOMs"
            isEnabled = $true
            value = $AppRoleValue
        }
        $allRoles = @()
        if ($apiApp.appRoles) { $allRoles += @($apiApp.appRoles) }
        $allRoles += $newRole

        $identifierUris = @()
        if ($apiApp.identifierUris) { $identifierUris += @($apiApp.identifierUris) }
        if ($identifierUris -notcontains $identifierUri) { $identifierUris += $identifierUri }

        $updateAppPath = Join-Path $tempDirectory "update-app.json"
        @{
            identifierUris = $identifierUris
            appRoles = $allRoles
        } | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $updateAppPath -Encoding UTF8

        az rest `
            --method PATCH `
            --url "$graphBase/applications/$apiAppObjectId" `
            --resource $graphResource `
            --headers "Content-Type=application/json" `
            --body "@$updateAppPath" `
            --output none
        Assert-AzSuccess "Creating API application role"
        Write-Host "Created application role '$AppRoleValue'." -ForegroundColor Green
    }

    if (@($apiApp.identifierUris) -notcontains $identifierUri) {
        $identifierUris = @()
        if ($apiApp.identifierUris) { $identifierUris += @($apiApp.identifierUris) }
        if ($identifierUris -notcontains $identifierUri) { $identifierUris += $identifierUri }

        $identifierPath = Join-Path $tempDirectory "identifier-uri.json"
        @{ identifierUris = $identifierUris } |
            ConvertTo-Json -Depth 10 |
            Set-Content -LiteralPath $identifierPath -Encoding UTF8

        az rest `
            --method PATCH `
            --url "$graphBase/applications/$apiAppObjectId" `
            --resource $graphResource `
            --headers "Content-Type=application/json" `
            --body "@$identifierPath" `
            --output none
        Assert-AzSuccess "Setting API identifier URI"
    }

    $servicePrincipals = @(
        az ad sp list `
            --display-name $DisplayName `
            --query "[].{id:id,appId:appId,displayName:displayName}" `
            --output json |
        ConvertFrom-Json
    )
    Assert-AzSuccess "Looking up API service principal"

    $apiServicePrincipal = @($servicePrincipals | Where-Object { $_.appId -eq $apiAppId }) | Select-Object -First 1
    if (-not $apiServicePrincipal) {
        $apiServicePrincipal = az ad sp create `
            --id $apiAppId `
            --query "{id:id,appId:appId,displayName:displayName}" `
            --output json |
        ConvertFrom-Json
        Assert-AzSuccess "Creating API service principal"
        Write-Host "Created API service principal." -ForegroundColor Green
    }
    else {
        Write-Host "API service principal already exists." -ForegroundColor Yellow
    }

    $apiServicePrincipalId = [string]$apiServicePrincipal.id
    if ([string]::IsNullOrWhiteSpace($apiServicePrincipalId)) {
        throw "The API service principal ID is empty."
    }

    $assignments = az rest `
        --method GET `
        --url "$graphBase/servicePrincipals/$QuickQuotePrincipalId/appRoleAssignments" `
        --resource $graphResource `
        --output json |
    ConvertFrom-Json
    Assert-AzSuccess "Reading Quick Quote app-role assignments"

    $existingAssignment = @(
        $assignments.value |
        Where-Object { $_.resourceId -eq $apiServicePrincipalId -and $_.appRoleId -eq $appRoleId }
    ) | Select-Object -First 1

    if (-not $existingAssignment) {
        $assignmentPath = Join-Path $tempDirectory "role-assignment.json"
        @{
            principalId = $QuickQuotePrincipalId
            resourceId = $apiServicePrincipalId
            appRoleId = $appRoleId
        } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $assignmentPath -Encoding UTF8

        az rest `
            --method POST `
            --url "$graphBase/servicePrincipals/$QuickQuotePrincipalId/appRoleAssignments" `
            --resource $graphResource `
            --headers "Content-Type=application/json" `
            --body "@$assignmentPath" `
            --output none
        Assert-AzSuccess "Assigning API role to Quick Quote"
        Write-Host "Assigned '$AppRoleValue' to Quick Quote." -ForegroundColor Green
    }
    else {
        Write-Host "Quick Quote already has '$AppRoleValue'." -ForegroundColor Yellow
    }

    $verifiedApp = az rest `
        --method GET `
        --url "$graphBase/applications/$apiAppObjectId" `
        --resource $graphResource `
        --output json |
    ConvertFrom-Json
    Assert-AzSuccess "Verifying API application"

    $verifiedRole = @(
        $verifiedApp.appRoles |
        Where-Object { $_.id -eq $appRoleId -and $_.value -eq $AppRoleValue -and $_.isEnabled -eq $true }
    ) | Select-Object -First 1
    if (-not $verifiedRole -or @($verifiedApp.identifierUris) -notcontains $identifierUri) {
        throw "The API role or identifier URI could not be verified."
    }

    $verifiedAssignments = az rest `
        --method GET `
        --url "$graphBase/servicePrincipals/$QuickQuotePrincipalId/appRoleAssignments" `
        --resource $graphResource `
        --output json |
    ConvertFrom-Json
    Assert-AzSuccess "Verifying Quick Quote assignment"

    $verifiedAssignment = @(
        $verifiedAssignments.value |
        Where-Object { $_.resourceId -eq $apiServicePrincipalId -and $_.appRoleId -eq $appRoleId }
    ) | Select-Object -First 1
    if (-not $verifiedAssignment) {
        throw "The Quick Quote app-role assignment could not be verified."
    }

    Write-Host ""
    Write-Host "SETUP VERIFIED" -ForegroundColor Green
    Write-Host "AgentQuote API application ID: $apiAppId"
    Write-Host "AgentQuote API object ID:      $apiAppObjectId"
    Write-Host "AgentQuote API audience:       $identifierUri"
    Write-Host "Quick Quote token scope:       $identifierUri/.default"
    Write-Host "Quick Quote allowed app ID:    $QuickQuoteClientId"
    Write-Host "API service principal ID:      $apiServicePrincipalId"
    Write-Host "Application role ID:           $appRoleId"
}
finally {
    # Cleanup must never hide the Graph/Azure error that caused setup to stop.
    try {
        if ([System.IO.Directory]::Exists($tempDirectory)) {
            [System.IO.Directory]::Delete($tempDirectory, $true)
        }
    }
    catch {
        Write-Warning "Temporary setup files could not be removed from '$tempDirectory'. Remove that directory manually."
    }
}
