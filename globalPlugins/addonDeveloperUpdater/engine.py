from __future__ import annotations

import ast
import ctypes
import json
import os
import re
import shutil
import tempfile
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

RELEASES_URL = "https://api.github.com/repos/nvaccess/nvda/releases?per_page=30"
RELEASES_FEED_URL = "https://github.com/nvaccess/nvda/releases.atom"
TAG_PATTERN = re.compile(r"(?:release-)?(20\d{2})\.(\d+)(?:\.(\d+))?(?:(?:alpha|beta|rc)\d+)?", re.I)
LAST_TESTED = re.compile(r"^(\s*lastTestedNVDAVersion\s*=\s*)([^\r\n#;]+)(.*)$", re.I | re.M)
VALUE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*)\s*=\s*(.*)$", re.M)
PRUNE_NAMES = {
    "$recycle.bin", "system volume information", "windows", "program files", "program files (x86)",
    "programdata", "appdata", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache",
    ".pytest_cache", "build", "dist", "outputs", "backups", ".nvdaaddonupdaterbackups",
    "runtime tests",
}


@dataclass(frozen=True)
class Release:
    tag: str
    manifest_version: str
    prerelease: bool
    url: str


@dataclass
class ProjectResult:
    name: str
    path: str
    status: str


def values(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8-sig")
    return {m.group(1).lower(): m.group(2).strip().strip('"\'') for m in VALUE.finditer(text)}


def version_tuple(value: str) -> tuple[int, int, int]:
    parts = [int(p) for p in value.strip().split(".")]
    return tuple((parts + [0, 0, 0])[:3])


def parse_release(item: dict) -> Release | None:
    tag = str(item.get("tag_name", ""))
    match = TAG_PATTERN.search(tag)
    if not match:
        return None
    patch = int(match.group(3) or 0)
    manifest_version = f"{int(match.group(1))}.{int(match.group(2))}" + (f".{patch}" if patch else "")
    return Release(tag.removeprefix("release-"), manifest_version, bool(item.get("prerelease")), str(item.get("html_url", "")))


def latest_release(include_prereleases: bool = True) -> Release:
    request = urllib.request.Request(RELEASES_URL, headers={"Accept": "application/vnd.github+json", "User-Agent": "NVDA-Addon-Developer-Updater/0.1"})
    last_error = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                items = json.load(response)
            for item in items:
                release = parse_release(item)
                if release and (include_prereleases or not release.prerelease):
                    return release
            raise RuntimeError("No matching NVDA release was returned by the API.")
        except Exception as error:
            last_error = error
            if attempt < 2:
                time.sleep(2 ** attempt)
    try:
        feed_request = urllib.request.Request(RELEASES_FEED_URL, headers={"User-Agent": "NVDA-Addon-Developer-Updater/0.1"})
        with urllib.request.urlopen(feed_request, timeout=30) as response:
            root = ET.parse(response).getroot()
        namespace = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall("atom:entry", namespace):
            title = entry.findtext("atom:title", default="", namespaces=namespace).strip()
            link_node = entry.find("atom:link", namespace)
            item = {
                "tag_name": title,
                "prerelease": bool(re.search(r"(?:alpha|beta|rc)\d*", title, re.I)),
                "html_url": link_node.get("href", "") if link_node is not None else "",
            }
            release = parse_release(item)
            if release and (include_prereleases or not release.prerelease):
                return release
    except Exception as feed_error:
        raise RuntimeError(f"GitHub API and releases feed failed: {last_error}; {feed_error}") from feed_error
    raise RuntimeError(f"No matching NVDA release was found; API error: {last_error}")


def drive_roots() -> list[Path]:
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    roots = []
    for index in range(26):
        if mask & (1 << index):
            root = Path(f"{chr(65 + index)}:\\")
            drive_type = ctypes.windll.kernel32.GetDriveTypeW(str(root))
            if drive_type in (2, 3):  # removable or fixed
                roots.append(root)
    return roots


def cloud_roots() -> list[Path]:
    candidates = []
    for variable in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial", "Dropbox", "GoogleDrive", "iCloudDrive"):
        value = os.environ.get(variable)
        if value:
            candidates.append(Path(value))
    home = Path.home()
    for name in ("OneDrive", "Dropbox", "Google Drive", "iCloudDrive"):
        path = home / name
        if path.exists():
            candidates.append(path)
    return candidates


def is_offline(path: Path) -> bool:
    try:
        attributes = ctypes.windll.kernel32.GetFileAttributesW(str(path))
        return attributes != -1 and bool(attributes & (0x1000 | 0x00400000))  # OFFLINE or RECALL_ON_DATA_ACCESS
    except Exception:
        return False


def should_prune(name: str) -> bool:
    lowered = name.lower()
    return lowered in PRUNE_NAMES or lowered.startswith("nvda-addon-test-")


def is_developer_manifest(path: Path) -> bool:
    if path.parent.name.lower() == "locale":
        return False
    current = path.parent
    for _ in range(4):
        if (current / ".git").is_dir() or (current / "buildVars.py").is_file() or (current / "sconstruct").is_file():
            return True
        if current.parent == current:
            break
        current = current.parent
    return False


def discover_manifests(roots: list[Path], cancelled=lambda: False) -> list[Path]:
    found: set[Path] = set()
    for root in roots:
        if cancelled():
            break
        if not root.exists() or is_offline(root):
            continue
        try:
            for directory, dirnames, filenames in os.walk(root, topdown=True, onerror=lambda _e: None):
                if cancelled():
                    return sorted(found)
                base = Path(directory)
                dirnames[:] = [d for d in dirnames if not should_prune(d) and not is_offline(base / d)]
                if "manifest.ini" in filenames:
                    manifest = base / "manifest.ini"
                    try:
                        metadata = values(manifest)
                        if metadata.get("name") and metadata.get("lasttestednvdaversion") and is_developer_manifest(manifest):
                            found.add(manifest.resolve())
                    except (OSError, UnicodeError):
                        pass
        except OSError:
            continue
    return sorted(found)


def validate(path: Path) -> list[str]:
    errors = []
    metadata = values(path)
    for key in ("name", "summary", "version", "minimumnvdaversion", "lasttestednvdaversion"):
        if not metadata.get(key):
            errors.append(f"missing {key}")
    for source in path.parent.rglob("*.py"):
        if any(part.lower() in PRUNE_NAMES or part == ".git" for part in source.parts):
            continue
        try:
            ast.parse(source.read_text(encoding="utf-8-sig"), filename=str(source))
        except (SyntaxError, UnicodeError) as error:
            errors.append(f"{source.name}: {error}")
    return errors


def update(path: Path, release: Release, backup_root: Path, apply_changes: bool = True) -> ProjectResult:
    metadata = values(path)
    name = metadata.get("name", path.parent.name)
    errors = validate(path)
    if errors:
        return ProjectResult(name, str(path), "validation failed: " + "; ".join(errors))
    current = metadata["lasttestednvdaversion"]
    if version_tuple(current) >= version_tuple(release.manifest_version):
        return ProjectResult(name, str(path), "current")
    if not apply_changes:
        return ProjectResult(name, str(path), f"update available: {current} to {release.manifest_version}")
    original = path.read_text(encoding="utf-8-sig")
    match = LAST_TESTED.search(original)
    if not match:
        return ProjectResult(name, str(path), "validation failed: compatibility key not found")
    backup = backup_root / release.tag / name / "manifest.ini"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, backup)
    changed = original[:match.start()] + f"{match.group(1)}{release.manifest_version}{match.group(3)}" + original[match.end():]
    fd, temporary = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            stream.write(changed)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return ProjectResult(name, str(path), f"updated to {release.manifest_version}")
