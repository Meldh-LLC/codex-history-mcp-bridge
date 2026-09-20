[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$Python = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    throw "The virtual environment is missing. Run .\setup.ps1 first."
}
& $Python (Join-Path $Root "doctor.py")
exit $LASTEXITCODE
