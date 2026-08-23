from __future__ import annotations

import ast, codecs, ctypes, hashlib, json, os, re, shutil, stat, tempfile, time
import urllib.error, urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

RELEASES_URL = "https://api.github.com/repos/nvaccess/nvda/releases?per_page=30"
RELEASES_FEED_URL = "https://github.com/nvaccess/nvda/releases.atom"
TAG_PATTERN = re.compile(r"(?:release-)?(20\d{2})\.(\d+)(?:\.(\d+))?(?:(alpha|beta|rc)(\d+))?", re.I)
LAST_TESTED = re.compile(r"^(\s*lastTestedNVDAVersion\s*=\s*)([^\r\n#;]+)(.*)$", re.I | re.M)
VALUE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*)\s*=\s*(.*)$", re.M)
PRUNE_NAMES = {"$recycle.bin", "system volume information", "windows", "program files", "program files (x86)", "programdata", "appdata", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache", ".pytest_cache", "build", "dist", "outputs", "backups", ".nvdaaddonupdaterbackups", "runtime tests"}

@dataclass(frozen=True)
class Release:
    tag: str
    manifest_version: str
    prerelease: bool
    url: str

@dataclass
class ProjectResult:
    project_id: str
    name: str
    path: str
    status: str

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def atomic_json_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

def read_json(path: Path | None, default=None):
    if path is None: return {} if default is None else default
    try:
        with path.open(encoding="utf-8-sig") as stream: return json.load(stream)
    except (OSError, ValueError, TypeError):
        return {} if default is None else default

def values(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8-sig")
    return {m.group(1).lower(): m.group(2).strip().strip('"\'') for m in VALUE.finditer(text)}

def version_tuple(value: str) -> tuple[int, int, int]:
    try:
        parts = [int(p) for p in value.strip().split(".")]
    except (TypeError, ValueError) as error:
        raise ValueError(f"Invalid NVDA version: {value}") from error
    if not 2 <= len(parts) <= 3: raise ValueError(f"Invalid NVDA version: {value}")
    return tuple((parts + [0])[:3])

def parse_release(item: dict) -> Release | None:
    tag = str(item.get("tag_name", "")); match = TAG_PATTERN.search(tag)
    if not match: return None
    patch = int(match.group(3) or 0)
    manifest_version = f"{int(match.group(1))}.{int(match.group(2))}" + (f".{patch}" if patch else "")
    return Release(tag.removeprefix("release-"), manifest_version, bool(item.get("prerelease")), str(item.get("html_url", "")))

def _select_release(items: list[dict], include_prereleases: bool) -> Release:
    releases = [r for item in items if (r := parse_release(item)) and (include_prereleases or not r.prerelease)]
    if not releases: raise RuntimeError("No matching NVDA release was returned.")
    def sort_key(release):
        match = TAG_PATTERN.search(release.tag)
        stage = (match.group(4) or "final").lower() if match else "final"
        stage_number = int(match.group(5) or 0) if match else 0
        return version_tuple(release.manifest_version), {"alpha": 0, "beta": 1, "rc": 2, "final": 3}[stage], stage_number
    return max(releases, key=sort_key)

def latest_release(include_prereleases: bool = True, cache_path: Path | None = None) -> Release:
    cache = read_json(cache_path, {}); headers = {"Accept": "application/vnd.github+json", "User-Agent": "NVDA-Addon-Developer-Updater/0.2"}
    if cache.get("etag"): headers["If-None-Match"] = cache["etag"]
    last_error = None
    for attempt in range(2):
        try:
            request = urllib.request.Request(RELEASES_URL, headers=headers)
            with urllib.request.urlopen(request, timeout=15) as response:
                release = _select_release(json.load(response), include_prereleases)
                if cache_path: atomic_json_write(cache_path, {"etag": response.headers.get("ETag"), "release": asdict(release), "checkedAt": utc_now()})
                return release
        except urllib.error.HTTPError as error:
            if error.code == 304 and cache.get("release"): return Release(**cache["release"])
            last_error = error
        except Exception as error: last_error = error
        if attempt == 0: time.sleep(1)
    try:
        request = urllib.request.Request(RELEASES_FEED_URL, headers={"User-Agent": "NVDA-Addon-Developer-Updater/0.2"})
        with urllib.request.urlopen(request, timeout=15) as response: root = ET.parse(response).getroot()
        ns = {"atom": "http://www.w3.org/2005/Atom"}; items = []
        for entry in root.findall("atom:entry", ns):
            title = entry.findtext("atom:title", default="", namespaces=ns).strip(); link = entry.find("atom:link", ns)
            items.append({"tag_name": title, "prerelease": bool(re.search(r"(?:alpha|beta|rc)\d*", title, re.I)), "html_url": link.get("href", "") if link is not None else ""})
        release = _select_release(items, include_prereleases)
        if cache_path: atomic_json_write(cache_path, {"release": asdict(release), "checkedAt": utc_now(), "apiError": str(last_error)})
        return release
    except Exception as feed_error:
        if cache.get("release"): return Release(**cache["release"])
        raise RuntimeError(f"GitHub API and releases feed failed: {last_error}; {feed_error}") from feed_error

def drive_roots() -> list[Path]:
    mask = ctypes.windll.kernel32.GetLogicalDrives(); roots = []
    for index in range(26):
        if mask & (1 << index):
            root = Path(f"{chr(65 + index)}:\\")
            if ctypes.windll.kernel32.GetDriveTypeW(str(root)) in (2, 3): roots.append(root)
    return roots

def cloud_roots() -> list[Path]:
    candidates = []
    for variable in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial", "Dropbox", "GoogleDrive", "iCloudDrive"):
        if value := os.environ.get(variable): candidates.append(Path(value))
    for name in ("OneDrive", "Dropbox", "Google Drive", "iCloudDrive"):
        if (path := Path.home() / name).exists(): candidates.append(path)
    return candidates

def default_development_roots() -> list[Path]:
    return [p for p in [Path.home() / "Documents" / "NVDA Add-on Development" / "Add-ons", *cloud_roots()] if p.exists()]

def is_offline(path: Path) -> bool:
    try:
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attrs != -1 and bool(attrs & (0x1000 | 0x00400000))
    except Exception: return False

def should_prune(name: str) -> bool:
    lowered = name.lower(); return lowered in PRUNE_NAMES or lowered.startswith("nvda-addon-test-")

def project_root(path: Path) -> Path | None:
    current = path.parent
    for _ in range(5):
        if (current / ".git").is_dir() or (current / "buildVars.py").is_file() or (current / "sconstruct").is_file(): return current.resolve()
        if current.parent == current: break
        current = current.parent
    return None

def project_id(path: Path) -> str:
    root = project_root(path) or path.parent.resolve()
    return hashlib.sha256(os.path.normcase(str(root)).encode("utf-8")).hexdigest()[:16]

def discover_manifests(roots: list[Path], cancelled=lambda: False) -> list[Path]:
    found: dict[tuple[str, str], Path] = {}
    for root in roots:
        if cancelled(): break
        if not root.exists() or is_offline(root): continue
        try:
            for directory, dirnames, filenames in os.walk(root, topdown=True, onerror=lambda _error: None):
                if cancelled(): return sorted(found.values())
                base = Path(directory); dirnames[:] = [n for n in dirnames if not should_prune(n) and not is_offline(base / n)]
                if "manifest.ini" not in filenames: continue
                manifest = base / "manifest.ini"
                if is_offline(manifest) or manifest.parent.name.lower() == "locale": continue
                try:
                    metadata = values(manifest); root_path = project_root(manifest)
                    if metadata.get("name") and metadata.get("lasttestednvdaversion") and root_path: found[(str(root_path).lower(), metadata["name"].lower())] = manifest.resolve()
                except (OSError, UnicodeError): pass
        except OSError: continue
    return sorted(found.values())

def validate(path: Path, cancelled=lambda: False) -> list[str]:
    errors = []; metadata = values(path)
    for key in ("name", "summary", "version", "minimumnvdaversion", "lasttestednvdaversion"):
        if not metadata.get(key): errors.append(f"missing {key}")
    for key in ("minimumnvdaversion", "lasttestednvdaversion"):
        if metadata.get(key):
            try: version_tuple(metadata[key])
            except ValueError as error: errors.append(str(error))
    for source in path.parent.rglob("*.py"):
        if cancelled(): return ["cancelled"]
        if any(should_prune(part) or part == ".git" for part in source.parts): continue
        try: ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
        except (SyntaxError, UnicodeError, OSError) as error: errors.append(f"{source.name}: {error}")
    return errors

def _write_manifest_preserving_format(path: Path, changed_text: str) -> None:
    original = path.read_bytes(); has_bom = original.startswith(codecs.BOM_UTF8); newline = "\r\n" if b"\r\n" in original else "\n"
    normalized = changed_text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", newline); output = normalized.encode("utf-8")
    if has_bom: output = codecs.BOM_UTF8 + output
    mode = stat.S_IMODE(path.stat().st_mode); fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream: stream.write(output)
        os.chmod(temporary, mode); os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

def update(path: Path, release: Release, backup_root: Path, apply_changes: bool = False, cancelled=lambda: False) -> ProjectResult:
    metadata = values(path); name = metadata.get("name", path.parent.name); identifier = project_id(path)
    if cancelled(): return ProjectResult(identifier, name, str(path), "cancelled")
    errors = validate(path, cancelled)
    if errors: return ProjectResult(identifier, name, str(path), "validation failed: " + "; ".join(errors))
    current = metadata["lasttestednvdaversion"]
    if version_tuple(current) >= version_tuple(release.manifest_version): return ProjectResult(identifier, name, str(path), "current")
    if not apply_changes: return ProjectResult(identifier, name, str(path), f"update available: {current} to {release.manifest_version}")
    original = path.read_text(encoding="utf-8-sig"); match = LAST_TESTED.search(original)
    if not match: return ProjectResult(identifier, name, str(path), "validation failed: compatibility key not found")
    backup = backup_root / release.tag / f"{name}-{identifier}" / "manifest.ini"; backup.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(path, backup)
    changed = original[:match.start()] + f"{match.group(1)}{release.manifest_version}{match.group(3)}" + original[match.end():]
    _write_manifest_preserving_format(path, changed)
    return ProjectResult(identifier, name, str(path), f"updated to {release.manifest_version}")
