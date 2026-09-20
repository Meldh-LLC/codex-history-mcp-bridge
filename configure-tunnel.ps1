[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^tunnel_')]
    [string]$TunnelId,

    [ValidatePattern('^[A-Za-z0-9_-]+$')]
    [string]$Profile = "codex-history"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
$Server = Join-Path $Root "server.py"

if (-not (Test-Path $Python)) {
    throw "The virtual environment is missing. Run .\setup.ps1 first."
}
if (-not (Test-Path $Server)) {
    throw "server.py is missing from $Root."
}
if ([string]::IsNullOrWhiteSpace($env:CONTROL_PLANE_API_KEY)) {
    throw 'CONTROL_PLANE_API_KEY is not set in this PowerShell session. Run .\load-runtime-key.ps1, then retry in the same PowerShell window.'
}

$LocalTunnelClient = Join-Path $Root "tunnel-client.exe"
if (Test-Path $LocalTunnelClient) {
    $TunnelClient = $LocalTunnelClient
} else {
    $TunnelCommand = Get-Command tunnel-client -ErrorAction SilentlyContinue
    if (-not $TunnelCommand) {
        throw "tunnel-client.exe was not found. Put it in this folder or add it to PATH. Download it from OpenAI Platform tunnel settings."
    }
    $TunnelClient = $TunnelCommand.Source
}

# tunnel-client expects one command string. Normalize Windows path separators to
# forward slashes before quoting; its command parser otherwise treats backslashes
# as escapes and can collapse paths such as E:\Tools\... into E:Tools....
$PythonForTunnel = $Python.Replace('\', '/')
$ServerForTunnel = $Server.Replace('\', '/')
$McpCommand = ('"{0}" "{1}"' -f $PythonForTunnel, $ServerForTunnel)

Write-Host "Creating/updating tunnel profile '$Profile'..."
& $TunnelClient init `
    --sample sample_mcp_stdio_local `
    --profile $Profile `
    --tunnel-id $TunnelId `
    --mcp-command $McpCommand
if ($LASTEXITCODE -ne 0) { throw "tunnel-client init failed." }

Write-Host ""
Write-Host "Validating the profile..."
& $TunnelClient doctor --profile $Profile --explain
if ($LASTEXITCODE -ne 0) { throw "tunnel-client doctor reported a failure." }

Write-Host ""
Write-Host "Profile '$Profile' is configured. Start it with:"
Write-Host ".\start-tunnel.ps1 -Profile $Profile"
