[CmdletBinding()]
param(
    [ValidatePattern('^[A-Za-z0-9_-]+$')]
    [string]$Profile = "codex-history"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path

if ([string]::IsNullOrWhiteSpace($env:CONTROL_PLANE_API_KEY)) {
    throw 'CONTROL_PLANE_API_KEY is not set in this PowerShell session. Run .\load-runtime-key.ps1, then retry in the same PowerShell window.'
}

$LocalTunnelClient = Join-Path $Root "tunnel-client.exe"
if (Test-Path $LocalTunnelClient) {
    $TunnelClient = $LocalTunnelClient
} else {
    $TunnelCommand = Get-Command tunnel-client -ErrorAction SilentlyContinue
    if (-not $TunnelCommand) {
        throw "tunnel-client.exe was not found. Put it in this folder or add it to PATH."
    }
    $TunnelClient = $TunnelCommand.Source
}

Write-Host "Starting OpenAI Secure MCP Tunnel profile '$Profile'."
Write-Host "Leave this window open while ChatGPT uses the Codex History Bridge."
& $TunnelClient run --profile $Profile
exit $LASTEXITCODE
