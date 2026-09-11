param(
	[string]$Version = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$arguments = @((Join-Path $projectRoot "tools\build_addon.py"))
if ($Version) { $arguments += @("--version", $Version) }

& python @arguments
if ($LASTEXITCODE -ne 0) { throw "Add-on build or audit failed with exit code $LASTEXITCODE" }
