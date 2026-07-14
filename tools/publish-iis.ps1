param(
    [string]$Destination = "\\SOIT-IIS.MANDELA.AC.ZA\GRP-04-09$",
    [switch]$Preview,
    [switch]$IncludeEnv
)

$ErrorActionPreference = "Stop"

$root = Resolve-Path (Join-Path $PSScriptRoot "..")
$excludeDirs = @(".git", ".agents", ".venv", "instance", "__pycache__")
$excludeFiles = @("*.pyc", "*.pyo", "*.db", ".env")

if (-not $IncludeEnv) {
    $excludeFiles += "env.txt"
}

$robocopyArgs = @(
    $root.Path,
    $Destination,
    "/E",
    "/R:2",
    "/W:3",
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

robocopy @robocopyArgs
$code = $LASTEXITCODE
if ($code -le 7) {
    exit 0
}
exit $code
