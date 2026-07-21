param(
    [string]$Destination = "\\SOIT-IIS.MANDELA.AC.ZA\GRP-04-09$",
    [switch]$Preview,
    [switch]$IncludeEnv,
    [switch]$IncludeVenv,
    [switch]$IncludePythonRuntime,
    [switch]$UploadOnly,
    [string]$PythonHome
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$excludeDirs = @(".git", ".agents", ".codex", ".vscode", ".idea", "instance", "__pycache__")
$excludeFiles = @("*.pyc", "*.pyo", "*.db", "*.log", "*.local", ".env", ".DS_Store")

if ($UploadOnly) {
    $IncludeEnv = $true
    $IncludeVenv = $true
    $IncludePythonRuntime = $true
}

if (-not $IncludeVenv) {
    $excludeDirs += ".venv"
}

if (-not $IncludePythonRuntime) {
    $excludeDirs += ".python"
}

if (-not $IncludeEnv) {
    $excludeFiles += "env.txt"
}

function Invoke-Robocopy {
    param(
        [string[]]$Arguments
    )

    robocopy @Arguments
    $code = $LASTEXITCODE
    if ($code -gt 7) {
        exit $code
    }
}

function Get-PythonHomeFromVenv {
    $cfg = Join-Path $root.Path ".venv\pyvenv.cfg"
    if (-not (Test-Path -LiteralPath $cfg)) {
        return $null
    }

    $line = Get-Content -LiteralPath $cfg | Where-Object { $_ -like "home = *" } | Select-Object -First 1
    if (-not $line) {
        return $null
    }
    return ($line -replace "^home = ", "").Trim()
}

$robocopyArgs = @(
    $root.Path,
    $Destination,
    "/E",
    "/R:2",
    "/W:3",
    "/NP",
    "/NFL",
    "/NDL",
    "/MT:16",
    "/XD"
)
$robocopyArgs += $excludeDirs
$robocopyArgs += "/XF"
$robocopyArgs += $excludeFiles

if ($Preview) {
    $robocopyArgs += "/L"
}

Write-Host "Publishing from $($root.Path)"
Write-Host "Publishing to   $Destination"
if ($Preview) {
    Write-Host "Preview mode: no files will be copied."
}
if (-not $IncludeEnv) {
    Write-Host "env.txt is excluded. Create it on the IIS server from env.production.example."
}
if ($UploadOnly) {
    Write-Host "Upload-only mode: env.txt, .venv and the local Python runtime will be uploaded."
}

Invoke-Robocopy -Arguments $robocopyArgs

if ($IncludePythonRuntime) {
    if (-not $PythonHome) {
        $PythonHome = Get-PythonHomeFromVenv
    }
    if (-not $PythonHome -or -not (Test-Path -LiteralPath (Join-Path $PythonHome "python.exe"))) {
        throw "Python runtime not found. Pass -PythonHome or create .venv locally first."
    }

    $pythonDestination = Join-Path $Destination ".python"
    $pythonArgs = @(
        $PythonHome,
        $pythonDestination,
        "/E",
        "/R:2",
        "/W:3",
        "/NP",
        "/NFL",
        "/NDL",
        "/MT:16",
        "/XF",
        "*.pyc",
        "*.pyo",
        "*.log"
    )
    if ($Preview) {
        $pythonArgs += "/L"
    }

    Write-Host "Publishing Python runtime from $PythonHome"
    Write-Host "Publishing Python runtime to   $pythonDestination"
    Invoke-Robocopy -Arguments $pythonArgs
}

exit 0
