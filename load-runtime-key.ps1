[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$Secret = Read-Host "Paste the tunnel runtime API key (input hidden; current PowerShell only)" -AsSecureString
$Pointer = [IntPtr]::Zero
try {
    $Pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secret)
    $Value = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Pointer)
    if ([string]::IsNullOrWhiteSpace($Value)) {
        throw "No runtime key supplied; existing environment was not changed."
    }
    $env:CONTROL_PLANE_API_KEY = $Value
    Write-Host "Runtime key loaded for this PowerShell process. It has not been saved to disk."
} finally {
    if ($Pointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Pointer)
    }
    $Value = $null
    if ($null -ne $Secret) { $Secret.Dispose() }
}
