[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

Write-Host "Codex History MCP Bridge setup"
Write-Host "Location: $Root"

$PyLauncher = Get-Command py -ErrorAction SilentlyContinue
$PythonCommand = Get-Command python -ErrorAction SilentlyContinue

if ($PyLauncher) {
    Write-Host "Creating Python virtual environment with py -3..."
    & $PyLauncher.Source -3 -m venv ".venv"
} elseif ($PythonCommand) {
    Write-Host "Creating Python virtual environment with python..."
    & $PythonCommand.Source -m venv ".venv"
} else {
    throw "Python 3.10 or newer was not found. Install Python, enable 'Add python.exe to PATH', then rerun this script."
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    throw "Virtual environment creation failed: $VenvPython does not exist."
}

$VersionText = & $VenvPython -c "import sys; print('.'.join(map(str, sys.version_info[:3])))"
$VersionParts = $VersionText.Trim().Split('.')
if ([int]$VersionParts[0] -lt 3 -or ([int]$VersionParts[0] -eq 3 -and [int]$VersionParts[1] -lt 10)) {
    throw "Python 3.10 or newer is required; found $VersionText."
}
Write-Host "Using Python $VersionText"

Write-Host "Using the virtual environment's bundled pip..."
& $VenvPython -m pip --version
if ($LASTEXITCODE -ne 0) { throw "pip is unavailable in the virtual environment." }

Write-Host "Installing the official MCP Python SDK..."
& $VenvPython -m pip install -r "requirements.txt"
if ($LASTEXITCODE -ne 0) { throw "Dependency installation failed." }

Write-Host "Running the local bridge doctor..."
& $VenvPython "doctor.py"
if ($LASTEXITCODE -ne 0) {
    Write-Warning "Dependencies are installed, but the Codex probe failed. Read README.md, especially the Codex executable and Windows/WSL sections."
    exit $LASTEXITCODE
}

Write-Host ""
Write-Host "Local setup passed. Next: create an OpenAI Secure MCP Tunnel, then run configure-tunnel.ps1."
