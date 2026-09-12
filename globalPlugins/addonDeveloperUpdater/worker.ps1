param(
	[Parameter(Mandatory = $true)][string]$RequestPath,
	[Parameter(Mandatory = $true)][string]$OutputPath
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
$prunedNames = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
@('$recycle.bin', 'system volume information', 'windows', 'program files', 'program files (x86)', 'programdata', 'appdata', 'node_modules', '.venv', 'venv', '__pycache__', '.tox', '.mypy_cache', '.pytest_cache', 'build', 'dist', 'outputs', 'backups', '.nvdaaddonupdaterbackups', 'runtime tests') | ForEach-Object { [void]$prunedNames.Add($_) }

function Write-WorkerResult([hashtable]$Result) {
	$json = $Result | ConvertTo-Json -Depth 8
	$tempPath = "$OutputPath.$PID.tmp"
	[System.IO.File]::WriteAllText($tempPath, $json, [System.Text.UTF8Encoding]::new($false))
	Move-Item -LiteralPath $tempPath -Destination $OutputPath -Force
}

function Test-Offline([string]$Path) {
	try {
		$attributes = [System.IO.File]::GetAttributes($Path)
		return (($attributes -band [System.IO.FileAttributes]::Offline) -ne 0) -or (($attributes -band 0x400000) -ne 0)
	} catch { return $true }
}

function Test-ProjectMarker([string]$ManifestPath) {
	$current = [System.IO.DirectoryInfo]::new([System.IO.Path]::GetDirectoryName($ManifestPath))
	for ($depth = 0; $depth -lt 5 -and $null -ne $current; $depth++) {
		if ([System.IO.File]::Exists((Join-Path $current.FullName '.git')) -or [System.IO.Directory]::Exists((Join-Path $current.FullName '.git')) -or [System.IO.File]::Exists((Join-Path $current.FullName 'buildVars.py')) -or [System.IO.File]::Exists((Join-Path $current.FullName 'sconstruct'))) { return $true }
		$current = $current.Parent
	}
	return $false
}

function Test-DeveloperManifest([string]$ManifestPath) {
	try {
		if (Test-Offline $ManifestPath) { return $false }
		$text = [System.IO.File]::ReadAllText($ManifestPath, [System.Text.Encoding]::UTF8)
		return ($text -match '(?im)^[ \t]*name[ \t]*=') -and ($text -match '(?im)^[ \t]*lastTestedNVDAVersion[ \t]*=') -and (Test-ProjectMarker $ManifestPath)
	} catch { return $false }
}

function Find-Manifests($Roots, [int]$MaxDirectories, [int]$MaxSeconds) {
	$found = [System.Collections.Generic.HashSet[string]]::new([System.StringComparer]::OrdinalIgnoreCase)
	$stack = [System.Collections.Generic.Stack[string]]::new()
	foreach ($root in $Roots) { if ($root) { $stack.Push([string]$root) } }
	$started = [System.Diagnostics.Stopwatch]::StartNew(); $visited = 0
	while ($stack.Count -gt 0 -and $visited -lt $MaxDirectories -and $started.Elapsed.TotalSeconds -lt $MaxSeconds) {
		$directory = $stack.Pop(); $visited++
		if (($visited % 25) -eq 0) { Start-Sleep -Milliseconds 10 }
		if (-not [System.IO.Directory]::Exists($directory) -or (Test-Offline $directory)) { continue }
		try {
			foreach ($file in [System.IO.Directory]::EnumerateFiles($directory, 'manifest.ini', [System.IO.SearchOption]::TopDirectoryOnly)) {
				if (([System.IO.Path]::GetFileName($file) -ieq 'manifest.ini') -and (Test-DeveloperManifest $file)) { [void]$found.Add([System.IO.Path]::GetFullPath($file)) }
			}
			foreach ($child in [System.IO.Directory]::EnumerateDirectories($directory)) {
				$name = [System.IO.Path]::GetFileName($child)
				if (-not $prunedNames.Contains($name) -and -not $name.StartsWith('nvda-addon-test-', [System.StringComparison]::OrdinalIgnoreCase) -and -not (Test-Offline $child)) { $stack.Push($child) }
			}
		} catch { continue }
	}
	return [pscustomobject]@{ manifests = @($found); directoriesVisited = $visited; truncated = ($stack.Count -gt 0); elapsedSeconds = [Math]::Round($started.Elapsed.TotalSeconds, 2) }
}

try {
	$request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
	if ($request.mode -eq 'background') {
		$manifests = @($request.manifestPaths | Where-Object { $_ -and [System.IO.File]::Exists([string]$_) -and -not (Test-Offline ([string]$_)) })
		$unavailableManifestPaths = @($request.manifestPaths | Where-Object { $_ -and (-not [System.IO.File]::Exists([string]$_) -or (Test-Offline ([string]$_))) })
		$directoriesVisited = 0; $truncated = $false; $elapsedSeconds = 0
	} else {
		$roots = @($request.roots)
		if ([bool]$request.fullSystem) {
			$roots += [System.IO.DriveInfo]::GetDrives() | Where-Object { $_.IsReady -and ($_.DriveType -eq 'Fixed' -or $_.DriveType -eq 'Removable') } | ForEach-Object { $_.RootDirectory.FullName }
		}
		$maxDirectories = if ($null -ne $request.maxDirectories) { [Math]::Max(1, [Math]::Min(20000, [int]$request.maxDirectories)) } else { 20000 }
		$maxSeconds = if ($null -ne $request.maxSeconds) { [Math]::Max(1, [Math]::Min(60, [int]$request.maxSeconds)) } else { 60 }
		$scan = Find-Manifests $roots $maxDirectories $maxSeconds; $manifests = @($scan.manifests); $directoriesVisited = $scan.directoriesVisited; $truncated = $scan.truncated; $elapsedSeconds = $scan.elapsedSeconds
	}
	Write-WorkerResult @{ ok = $true; manifests = @($manifests); unavailableManifestPaths = @($unavailableManifestPaths); directoriesVisited = $directoriesVisited; truncated = $truncated; elapsedSeconds = $elapsedSeconds; completedAt = [DateTime]::UtcNow.ToString('o') }
} catch {
	Write-WorkerResult @{ ok = $false; error = $_.Exception.Message; manifests = @(); completedAt = [DateTime]::UtcNow.ToString('o') }
	exit 1
}
