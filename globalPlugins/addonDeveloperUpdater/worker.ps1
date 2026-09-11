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

function Get-LatestRelease([bool]$IncludePrereleases) {
	$headers = @{ Accept = 'application/vnd.github+json'; 'User-Agent' = 'NVDA-Addon-Developer-Updater' }
	$items = Invoke-RestMethod -Uri 'https://api.github.com/repos/nvaccess/nvda/releases?per_page=30' -Headers $headers -TimeoutSec 15
	$releases = foreach ($item in $items) {
		$tag = [string]$item.tag_name
		if ($tag -notmatch '^(?:release-)?(20\d{2})\.(\d+)(?:\.(\d+))?(?:(alpha|beta|rc)(\d+))?$') { continue }
		$stage = if ($Matches[4]) { $Matches[4].ToLowerInvariant() } else { 'final' }
		$isPrerelease = [bool]$item.prerelease -or $stage -ne 'final'
		if (-not $IncludePrereleases -and $isPrerelease) { continue }
		$year = [int]$Matches[1]; $minor = [int]$Matches[2]; $patch = if ($Matches[3]) { [int]$Matches[3] } else { 0 }
		$stageRank = @{ alpha = 0; beta = 1; rc = 2; final = 3 }[$stage]
		$stageNumber = if ($Matches[5]) { [int]$Matches[5] } else { 0 }
		$manifestVersion = "$year.$minor" + $(if ($patch -gt 0) { ".$patch" } else { '' })
		[pscustomobject]@{ tag = ($tag -replace '^(?i:release-)', ''); manifestVersion = $manifestVersion; prerelease = $isPrerelease; url = [string]$item.html_url; key = '{0:D4}.{1:D6}.{2:D6}.{3:D1}.{4:D6}' -f $year, $minor, $patch, $stageRank, $stageNumber }
	}
	if ($IncludePrereleases) {
		try {
			$alphaIndex = (Invoke-WebRequest -Uri 'https://download.nvaccess.org/snapshots/alpha/' -Headers $headers -TimeoutSec 15 -UseBasicParsing).Content
			$buildSource = (Invoke-WebRequest -Uri 'https://raw.githubusercontent.com/nvaccess/nvda/master/source/buildVersion.py' -Headers $headers -TimeoutSec 15 -UseBasicParsing).Content
			$alphaMatches = [regex]::Matches($alphaIndex, 'nvda_snapshot_alpha-(\d+),([0-9a-f]+)\.exe', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
			if ($alphaMatches.Count -gt 0 -and $buildSource -match '(?m)^version_year\s*=\s*(\d+)\s*$') {
				$alphaYear = [int]$Matches[1]
				if ($buildSource -notmatch '(?m)^version_major\s*=\s*(\d+)\s*$') { throw 'NVDA alpha major version was not found' }; $alphaMajor = [int]$Matches[1]
				if ($buildSource -notmatch '(?m)^version_minor\s*=\s*(\d+)\s*$') { throw 'NVDA alpha minor version was not found' }; $alphaMinor = [int]$Matches[1]
				$latestAlpha = $alphaMatches | Sort-Object { [int]$_.Groups[1].Value } -Descending | Select-Object -First 1
				$alphaBuild = [int]$latestAlpha.Groups[1].Value; $alphaCommit = $latestAlpha.Groups[2].Value
				$alphaManifest = "$alphaYear.$alphaMajor" + $(if ($alphaMinor -gt 0) { ".$alphaMinor" } else { '' })
				$releases += [pscustomobject]@{ tag = "alpha-$alphaBuild,$alphaCommit"; manifestVersion = $alphaManifest; prerelease = $true; url = "https://download.nvaccess.org/snapshots/alpha/nvda_snapshot_alpha-$alphaBuild,$alphaCommit.exe"; key = '{0:D4}.{1:D6}.{2:D6}.{3:D1}.{4:D6}' -f $alphaYear, $alphaMajor, $alphaMinor, 0, $alphaBuild }
			}
		} catch { Write-Warning "NVDA alpha snapshot lookup failed: $_" }
	}
	$selected = $releases | Sort-Object key -Descending | Select-Object -First 1
	if ($null -eq $selected) { throw 'No matching NVDA release was returned.' }
	return $selected
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
	return @($found)
}

try {
	$request = Get-Content -LiteralPath $RequestPath -Raw -Encoding UTF8 | ConvertFrom-Json
	$release = Get-LatestRelease ([bool]$request.includePrereleases)
	if ($request.mode -eq 'background') {
		$manifests = @($request.manifestPaths | Where-Object { $_ -and [System.IO.File]::Exists([string]$_) -and -not (Test-Offline ([string]$_)) })
	} else {
		$roots = @($request.roots)
		if ([bool]$request.fullSystem) {
			$roots += [System.IO.DriveInfo]::GetDrives() | Where-Object { $_.IsReady -and ($_.DriveType -eq 'Fixed' -or $_.DriveType -eq 'Removable') } | ForEach-Object { $_.RootDirectory.FullName }
		}
		$manifests = Find-Manifests $roots 20000 60
	}
	Write-WorkerResult @{ ok = $true; releaseTag = $release.tag; manifestVersion = $release.manifestVersion; prerelease = $release.prerelease; releaseUrl = $release.url; manifests = @($manifests); completedAt = [DateTime]::UtcNow.ToString('o') }
} catch {
	Write-WorkerResult @{ ok = $false; error = $_.Exception.Message; manifests = @(); completedAt = [DateTime]::UtcNow.ToString('o') }
	exit 1
}
