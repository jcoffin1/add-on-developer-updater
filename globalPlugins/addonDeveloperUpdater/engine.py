from __future__ import annotations

import codecs, ctypes, hashlib, json, os, re, shutil, stat, subprocess, tempfile, time
import urllib.error, urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

try:
    from . import python_compatibility
except ImportError:
    # Unit tests and the build tool import these modules directly from their folder.
    import python_compatibility

RELEASES_URL = "https://api.github.com/repos/nvaccess/nvda/releases?per_page=30"
RELEASES_FEED_URL = "https://github.com/nvaccess/nvda/releases.atom"
ALPHA_SNAPSHOTS_URL = "https://download.nvaccess.org/snapshots/alpha/"
BUILD_VERSION_URL = "https://raw.githubusercontent.com/nvaccess/nvda/master/source/buildVersion.py"
USER_AGENT = "NVDA-Addon-Developer-Updater"
TAG_PATTERN = re.compile(r"^(?:release-)?(20\d{2})\.(\d+)(?:\.(\d+))?(?:(alpha|beta|rc)(\d+))?$", re.I)
LAST_TESTED = re.compile(r"^([ \t]*lastTestedNVDAVersion[ \t]*=[ \t]*)(?P<quote>[\"']?)(20\d{2}\.\d+(?:\.\d+)?)(?P=quote)(?P<suffix>[ \t]*(?:[#;].*)?)(?P<cr>\r?)$", re.I | re.M)
VALUE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*)\s*=\s*(.*)$", re.M)
TRIPLE_VALUE = re.compile(r"^[ \t]*([A-Za-z][A-Za-z0-9]*)[ \t]*=[ \t]*(?P<quote>\"\"\"|''')(?P<value>.*?)(?P=quote)[ \t]*(?:[#;].*)?$", re.M | re.S)
# Keep this list identical to worker.ps1, which performs discovery inside NVDA.
PRUNE_NAMES = {".git", ".hg", ".svn", ".vs", ".vscode", ".idea", ".ruff_cache", ".nox", ".eggs", "site-packages", "$recycle.bin", "system volume information", "windows", "program files", "program files (x86)", "programdata", "appdata", "node_modules", ".venv", "venv", "__pycache__", ".tox", ".mypy_cache", ".pytest_cache", "build", "dist", "outputs", "backups", ".nvdaaddonupdaterbackups", "runtime tests"}
MAX_PYTHON_FILE_BYTES = 5 * 1024 * 1024
MAX_VALIDATION_ERRORS = 100
WINDOWS_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}

@dataclass(frozen=True)
class Release:
    tag: str
    manifest_version: str
    prerelease: bool
    url: str
    source: str = "live"
    checked_at: str = ""
    build_version: str = ""

@dataclass
class ProjectResult:
    project_id: str
    name: str
    path: str
    status: str
    backup_path: str = ""
    previous_last_tested: str = ""
    target_last_tested: str = ""
    target_manifest_hash: str = ""
    minimum_version: str = ""
    branch: str = ""
    manifest_changed: bool = False
    addon_version: str = ""
    # Details that tell copies of the same add-on apart in the tracking window.
    summary: str = ""
    remote: str = ""
    uncommitted_changes: int = -1
    unpushed_commits: int = -1
    last_change: str = ""

@dataclass(frozen=True)
class CompatibilityTargetProject:
    project_id: str
    name: str
    path: str
    current_version: str
    minimum_version: str
    update_channel: str
    branch: str
    manifest_changed: bool

def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()

def automatic_check_delay(state: dict, interval_minutes: int, now: datetime | None = None) -> float:
    """Return seconds until the next check, including bounded failure backoff."""
    base = max(900, int(interval_minutes) * 60)
    try: failures = max(0, min(4, int(state.get("automaticFailureCount", 0) or 0)))
    except (AttributeError, TypeError, ValueError): failures = 0
    interval = min(max(21600, base), base * (2 ** failures))
    try:
        current = now or datetime.now(timezone.utc); elapsed = (current - datetime.fromisoformat(state["lastCheckAt"])).total_seconds()
        return max(30, interval - max(0, elapsed))
    except (KeyError, TypeError, ValueError):
        return 30

def rescan_due(state: dict, hours: int, now: datetime | None = None) -> bool:
    """Return whether periodic discovery should run, treating clock jumps as due."""
    try:
        elapsed = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(state.get("lastDiscoveryAt") or state["lastScanAt"])).total_seconds()
        return elapsed < -300 or elapsed >= hours * 3600
    except (AttributeError, KeyError, TypeError, ValueError): return True

def worker_mode(manual: bool, state: dict, hours: int, now: datetime | None = None) -> str:
    """Choose manual discovery, periodic discovery, or a check of saved manifests only."""
    if manual: return "manual"
    return "periodic" if rescan_due(state, hours, now) else "background"

def submission_channel(release: Release) -> str:
    """Return the Store channel implied by updating to an NVDA build."""
    return "dev" if release_channel(release) == "alpha" else ("beta" if release.prerelease else "stable")

_SENTENCE_BREAK = re.compile(r"(?<=[.?!])\s+(?=[\"'(\[]?[A-Z0-9])")

def readable_text(text: str) -> str:
    """Lay out a message for speech and braille: one sentence per line, one list item per line.

    NVDA speaks the first line when a message field gains focus, and a braille
    display shows one line at a time, so the outcome must stand alone on line one.
    Text that already contains line breaks is treated as deliberately formatted.
    """
    text = str(text or "").strip()
    if not text or "\n" in text:
        return text
    lines = []
    for sentence in _SENTENCE_BREAK.split(text):
        head, separator, rest = sentence.partition(": ")
        items = [item.strip() for item in rest.split("; ")] if separator else []
        if len(items) >= 2 and "; " not in head:
            lines.append(head + ":"); lines.extend(items)
        else:
            lines.append(sentence.strip())
    return "\n".join(lines)

def first_line(text: str) -> str:
    """Return the line NVDA speaks first, for announcing an updated message."""
    return next((line.strip() for line in str(text or "").splitlines() if line.strip()), "")

def readable_message(summary: str, details=(), next_step: str = "") -> str:
    """Build a message whose first line is the outcome, followed by one fact per line."""
    # An empty detail separates groups with one blank line; repeated or edge blanks are dropped.
    lines = [summary.strip(), ""]
    for detail in [*details, "", next_step]:
        text = str(detail or "").strip()
        if text or lines[-1]: lines.append(text)
    while lines and not lines[-1]: lines.pop()
    return "\n".join(lines)

def information_window_text(message: str, previous_title: str, previous_text: str, earlier_label: str = "Earlier result", limit: int = 20000) -> str:
    """Put a new result first while keeping an unread earlier result below it."""
    separator = "\n\n"
    if not previous_text or previous_text == message:
        return message
    if previous_text.startswith(message + separator):
        # A repeated result is already shown first; keep what follows it.
        return previous_text
    combined = f"{message}{separator}{earlier_label}, {previous_title}:\n{previous_text}"
    return combined if len(combined) <= limit else combined[:limit].rsplit("\n", 1)[0]

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
        with path.open(encoding="utf-8-sig") as stream:
            value = json.load(stream)
            return value if isinstance(value, dict) else ({} if default is None else default)
    except (OSError, ValueError, TypeError):
        return {} if default is None else default

def _manifest_value(value: str) -> str:
    value = value.strip()
    if value[:1] in ("\"", "'"):
        quote = value[0]; end = value.find(quote, 1)
        if end >= 0: return value[1:end]
    return re.split(r"\s[;#]", value, maxsplit=1)[0].strip().strip('"\'')

def values(path: Path) -> dict[str, str]:
    text = path.read_text(encoding="utf-8-sig")
    fields = {m.group(1).lower(): _manifest_value(m.group(2)) for m in VALUE.finditer(text)}
    # NVDA manifests permit ConfigObj-style triple-quoted descriptions and
    # changelogs.  The ordinary line parser sees only the opening quotes, so
    # replace those values with the complete field after the initial pass.
    fields.update({match.group(1).lower(): match.group("value").strip() for match in TRIPLE_VALUE.finditer(text)})
    return fields

def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not re.fullmatch(r"20\d{2}\.\d+(?:\.\d+)?", value.strip()):
        raise ValueError(f"Invalid NVDA version: {value}")
    parts = [int(p) for p in value.strip().split(".")]
    return tuple((parts + [0])[:3])

def compatibility_version_choices(api_versions: dict) -> list[tuple[str, bool]]:
    """Return unique NVDA API versions in newest-first display form."""
    choices = {}
    for version, details in api_versions.items() if isinstance(api_versions, dict) else ():
        try:
            numeric = version_tuple(version)
        except ValueError:
            continue
        display = f"{numeric[0]}.{numeric[1]}" + (f".{numeric[2]}" if numeric[2] else "")
        experimental = bool(details.get("experimental")) if isinstance(details, dict) else False
        choices[display] = choices.get(display, False) or experimental
    return sorted(choices.items(), key=lambda item: version_tuple(item[0]), reverse=True)

def allowed_compatibility_targets(projects: list[CompatibilityTargetProject], choices: list[tuple[str, bool]]) -> list[tuple[str, bool]]:
    """Return official versions valid for every selected project."""
    if not projects:
        return []
    allowed = []
    for choice in choices:
        try:
            channel_allows_target = not choice[1] or all(project.update_channel.casefold() in {"beta", "dev"} for project in projects)
            if channel_allows_target and all(version_tuple(project.minimum_version) <= version_tuple(choice[0]) < version_tuple(project.current_version) for project in projects):
                allowed.append(choice)
        except ValueError:
            continue
    return allowed

def parse_release(item: dict) -> Release | None:
    if not isinstance(item, dict): return None
    tag = str(item.get("tag_name", "")).strip()
    match = TAG_PATTERN.fullmatch(tag)
    if not match: return None
    patch = int(match.group(3) or 0)
    manifest_version = f"{int(match.group(1))}.{int(match.group(2))}" + (f".{patch}" if patch else "")
    clean_tag = tag[8:] if tag.lower().startswith("release-") else tag
    return Release(clean_tag, manifest_version, bool(item.get("prerelease")) or bool(match.group(4)), str(item.get("html_url", "")), build_version=clean_tag)

def parse_alpha_snapshot(index_html: str, build_version_source: str) -> Release | None:
    snapshots = [(int(build), commit) for build, commit in re.findall(r"nvda_snapshot_alpha-(\d+),([0-9a-f]+)\.exe", index_html, re.I)]
    fields = {}
    for name in ("year", "major", "minor"):
        match = re.search(rf"(?m)^version_{name}\s*=\s*(\d+)\s*$", build_version_source)
        if not match: return None
        fields[name] = int(match.group(1))
    if not snapshots: return None
    build, commit = max(snapshots)
    manifest = f"{fields['year']}.{fields['major']}" + (f".{fields['minor']}" if fields["minor"] else "")
    full_version = f"{fields['year']}.{fields['major']}.{fields['minor']}.{build}"
    return Release(f"alpha-{build},{commit}", manifest, True, ALPHA_SNAPSHOTS_URL + f"nvda_snapshot_alpha-{build},{commit}.exe", build_version=full_version)

def release_channel(release: Release) -> str:
    """Return the user-facing NVDA release channel for an exact build."""
    if release.tag.casefold().startswith("alpha-"):
        return "alpha"
    match = TAG_PATTERN.fullmatch(release.tag)
    return (match.group(4) or "stable").casefold() if match else ("prerelease" if release.prerelease else "stable")

def _release_sort_key(release: Release):
    alpha = re.fullmatch(r"alpha-(\d+),[0-9a-f]+", release.tag, re.I)
    if alpha:
        return version_tuple(release.manifest_version), 0, int(alpha.group(1))
    match = TAG_PATTERN.search(release.tag)
    stage = (match.group(4) or "final").lower() if match else "final"
    stage_number = int(match.group(5) or 0) if match else 0
    return version_tuple(release.manifest_version), {"alpha": 0, "beta": 1, "rc": 2, "final": 3}[stage], stage_number

def _select_release(items: list[dict], include_prereleases: bool) -> Release:
    releases = [r for item in items if (r := parse_release(item)) and (include_prereleases or not r.prerelease)]
    if not releases: raise RuntimeError("No matching NVDA release was returned.")
    return max(releases, key=_release_sort_key)

def _latest_alpha_release() -> Release | None:
    headers = {"User-Agent": USER_AGENT}
    with urllib.request.urlopen(urllib.request.Request(ALPHA_SNAPSHOTS_URL, headers=headers), timeout=15) as response:
        index_html = response.read().decode("utf-8", errors="replace")
    with urllib.request.urlopen(urllib.request.Request(BUILD_VERSION_URL, headers=headers), timeout=15) as response:
        build_source = response.read().decode("utf-8", errors="replace")
    return parse_alpha_snapshot(index_html, build_source)

def _release_feed_items() -> list[dict]:
    request = urllib.request.Request(RELEASES_FEED_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=15) as response:
        return parse_release_feed(ET.parse(response).getroot())

def parse_release_feed(root) -> list[dict]:
    """Read release tags from Atom entry IDs, falling back to titles.

    Entry IDs end with the Git tag, such as .../release-2026.3beta4, so they stay
    reliable even if NV Access gives a release a descriptive title.
    """
    namespace = {"atom": "http://www.w3.org/2005/Atom"}; items = []
    for entry in root.findall("atom:entry", namespace):
        title = entry.findtext("atom:title", default="", namespaces=namespace).strip(); link = entry.find("atom:link", namespace)
        identifier = entry.findtext("atom:id", default="", namespaces=namespace).strip()
        id_tag = identifier.rsplit("/", 1)[-1] if "/" in identifier else ""
        tag = next((candidate for candidate in (id_tag, title) if TAG_PATTERN.fullmatch(candidate)), title)
        items.append({"tag_name": tag, "prerelease": bool(re.search(r"(?:alpha|beta|rc)\d*", tag, re.I)), "html_url": link.get("href", "") if link is not None else ""})
    return items

def _release_with_status(release: Release, source: str, checked_at: str) -> Release:
    return Release(release.tag, release.manifest_version, release.prerelease, release.url, source, checked_at, release.build_version)

def _release_choices(items: list[dict], include_prereleases: bool, alpha: Release | None = None, source="live", checked_at="") -> list[Release]:
    """Return the newest meaningful build from each available release channel."""
    newest = {}
    for item in items:
        release = parse_release(item)
        if release is None:
            continue
        channel = release_channel(release)
        if channel != "stable" and not include_prereleases:
            continue
        if channel not in newest or _release_sort_key(release) > _release_sort_key(newest[channel]):
            newest[channel] = release
    if include_prereleases and alpha is not None:
        newest["alpha"] = alpha
    stable = newest.get("stable")
    prereleases = [release for channel, release in newest.items() if channel != "stable"]
    if stable is not None:
        # Once a family is stable, its older beta, RC, or alpha builds are no
        # longer useful compatibility targets.
        prereleases = [release for release in prereleases if _release_sort_key(release) > _release_sort_key(stable)]
    ordered = ([stable] if stable is not None else []) + sorted(prereleases, key=_release_sort_key, reverse=True)
    return [_release_with_status(release, release.source if release.source not in {"", "live"} else source, release.checked_at or checked_at) for release in ordered]

def _cached_release_choices(cache: dict, include_prereleases: bool) -> list[Release]:
    raw = cache.get("availableReleases", []) if isinstance(cache, dict) else []
    choices = []
    for item in raw if isinstance(raw, list) else []:
        try:
            release = Release(**item)
            version_tuple(release.manifest_version)
            if include_prereleases or not release.prerelease:
                choices.append(_release_with_status(release, "cached", release.checked_at or str(cache.get("availableCheckedAt", ""))))
        except (TypeError, ValueError):
            continue
    if choices:
        return choices
    # Migrate the older two-entry cache format without losing offline use.
    fallback = []
    for include in (False, True) if include_prereleases else (False,):
        release = _cached_release(cache, include)
        if release is not None and all(existing.tag.casefold() != release.tag.casefold() for existing in fallback):
            fallback.append(release)
    return fallback

def available_releases(include_prereleases: bool = True, cache_path: Path | None = None) -> list[Release]:
    """Load selectable stable and prerelease builds, with an offline cache fallback."""
    cache = read_json(cache_path, {}); headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    last_error = None
    for attempt in range(2):
        try:
            with urllib.request.urlopen(urllib.request.Request(RELEASES_URL, headers=headers), timeout=15) as response:
                items = json.load(response)
            checked_at = utc_now(); alpha = None
            if include_prereleases:
                try:
                    alpha = _latest_alpha_release()
                except Exception:
                    alpha = next((release for release in _cached_release_choices(cache, True) if release_channel(release) == "alpha"), None)
            choices = _release_choices(items, include_prereleases, alpha, "live", checked_at)
            if not choices:
                raise RuntimeError("No selectable NVDA releases were returned.")
            _save_release_cache(cache_path, {**cache, "availableReleases": [asdict(release) for release in choices], "availableCheckedAt": checked_at})
            return choices
        except Exception as error:
            last_error = error
        if attempt == 0:
            time.sleep(1)
    try:
        checked_at = utc_now(); alpha = None
        if include_prereleases:
            try:
                alpha = _latest_alpha_release()
            except Exception:
                alpha = next((release for release in _cached_release_choices(cache, True) if release_channel(release) == "alpha"), None)
        choices = _release_choices(_release_feed_items(), include_prereleases, alpha, "release feed", checked_at)
        if not choices: raise RuntimeError("The official NVDA releases feed returned no selectable builds.")
        _save_release_cache(cache_path, {**cache, "availableReleases": [asdict(release) for release in choices], "availableCheckedAt": checked_at, "apiError": str(last_error)})
        return choices
    except Exception as feed_error:
        if cached := _cached_release_choices(cache, include_prereleases):
            return cached
        raise RuntimeError(f"Selectable NVDA releases could not be loaded: {last_error}; {feed_error}") from feed_error

def matching_release(choice: Release, available: list[Release]) -> Release | None:
    """Return the refreshed copy of an exact user-selected build."""
    return next((release for release in available if release.tag.casefold() == choice.tag.casefold() and release.manifest_version == choice.manifest_version), None)

def save_manual_review(state: dict, release: Release, results: list[ProjectResult], merge: bool = False) -> None:
    """Persist pending manual results independently from background scan state."""
    available = [result for result in results if result.status.startswith("update available: ")]
    if merge:
        saved_release, saved_results = load_manual_review(state)
        if saved_release is not None and saved_release.tag.casefold() == release.tag.casefold() and saved_release.manifest_version == release.manifest_version:
            by_project = {result.project_id: result for result in saved_results}
            by_project.update({result.project_id: result for result in available})
            available = list(by_project.values())
    if not available:
        if not merge:
            state.pop("manualUpdateReview", None)
        return
    state["manualUpdateReview"] = {"release": asdict(release), "results": [asdict(result) for result in available]}

def load_manual_review(state: dict) -> tuple[Release | None, list[ProjectResult]]:
    """Load a valid pending manual review, ignoring damaged legacy state."""
    saved = state.get("manualUpdateReview") if isinstance(state, dict) else None
    if not isinstance(saved, dict) or not isinstance(saved.get("release"), dict) or not isinstance(saved.get("results"), list):
        return None, []
    try:
        release = Release(**saved["release"])
        version_tuple(release.manifest_version)
    except (TypeError, ValueError):
        return None, []
    results = []
    for item in saved["results"]:
        try:
            result = ProjectResult(**item)
            if result.status.startswith("update available: "):
                results.append(result)
        except (TypeError, ValueError):
            continue
    return (release, results) if results else (None, [])

def finish_manual_review_items(state: dict, project_ids: set[str]) -> None:
    """Remove only successfully completed projects from a pending manual review."""
    release, results = load_manual_review(state)
    if release is None:
        state.pop("manualUpdateReview", None)
        return
    remaining = [result for result in results if result.project_id not in project_ids]
    if remaining:
        state["manualUpdateReview"] = {"release": asdict(release), "results": [asdict(result) for result in remaining]}
    else:
        state.pop("manualUpdateReview", None)

def _save_release_cache(path: Path | None, data: dict) -> None:
    if path is None:
        return
    try:
        atomic_json_write(path, data)
    except OSError:
        # A valid network result remains usable even if the configuration
        # directory is temporarily read-only or full.
        pass

def _cached_release(cache: dict, include_prereleases: bool) -> Release | None:
    key = "prerelease" if include_prereleases else "stable"
    candidate = cache.get("releases", {}).get(key) if isinstance(cache.get("releases"), dict) else None
    if candidate is None: candidate = cache.get("release")
    if not isinstance(candidate, dict): return None
    try:
        release = Release(**candidate)
        version_tuple(release.manifest_version)
        if not include_prereleases and release.prerelease: return None
        return _release_with_status(release, "cached", release.checked_at or str(cache.get("checkedAt", "")))
    except (TypeError, ValueError): return None

def latest_release(include_prereleases: bool = True, cache_path: Path | None = None) -> Release:
    cache = read_json(cache_path, {}); headers = {"Accept": "application/vnd.github+json", "User-Agent": USER_AGENT}
    if cache.get("etag"): headers["If-None-Match"] = cache["etag"]
    last_error = None
    for attempt in range(2):
        try:
            request = urllib.request.Request(RELEASES_URL, headers=headers)
            with urllib.request.urlopen(request, timeout=15) as response:
                items = json.load(response); checked_at = utc_now(); prerelease = _select_release(items, True); prerelease_source = "live"; prerelease_checked_at = checked_at
                if include_prereleases:
                    try:
                        alpha = _latest_alpha_release()
                        if alpha is not None and _release_sort_key(alpha) > _release_sort_key(prerelease): prerelease = alpha
                    except Exception:
                        cached_prerelease = _cached_release(cache, True)
                        if cached_prerelease is not None and _release_sort_key(cached_prerelease) > _release_sort_key(prerelease):
                            prerelease = cached_prerelease; prerelease_source = "cached"; prerelease_checked_at = cached_prerelease.checked_at
                try: stable = _select_release(items, False)
                except RuntimeError:
                    if not include_prereleases: raise
                    stable = None
                release = prerelease if include_prereleases else stable
                cached_releases = {"prerelease": asdict(_release_with_status(prerelease, prerelease_source, prerelease_checked_at))}
                if stable is not None: cached_releases["stable"] = asdict(_release_with_status(stable, "live", checked_at))
                response_headers = getattr(response, "headers", {})
                _save_release_cache(cache_path, {**cache, "etag": response_headers.get("ETag"), "releases": cached_releases, "checkedAt": checked_at})
                return _release_with_status(release, prerelease_source, prerelease_checked_at) if include_prereleases else _release_with_status(release, "live", checked_at)
        except urllib.error.HTTPError as error:
            if error.code == 304 and (cached := _cached_release(cache, include_prereleases)):
                if include_prereleases:
                    try:
                        alpha = _latest_alpha_release()
                        if alpha is not None and _release_sort_key(alpha) > _release_sort_key(cached):
                            checked_at = utc_now(); cached_releases = dict(cache.get("releases", {})); cached_releases["prerelease"] = asdict(alpha)
                            _save_release_cache(cache_path, {**cache, "releases": cached_releases, "checkedAt": checked_at})
                            return _release_with_status(alpha, "live", checked_at)
                    except Exception:
                        return cached
                checked_at = utc_now(); validated = _release_with_status(cached, "validated cache", checked_at); cached_releases = dict(cache.get("releases", {})); cached_releases["prerelease" if include_prereleases else "stable"] = asdict(validated)
                _save_release_cache(cache_path, {**cache, "releases": cached_releases, "checkedAt": checked_at})
                return validated
            last_error = error
        except Exception as error: last_error = error
        if attempt == 0: time.sleep(1)
    try:
        items = _release_feed_items()
        release = _select_release(items, include_prereleases); release_source = "release feed"; release_checked_at = utc_now()
        if include_prereleases:
            try:
                alpha = _latest_alpha_release()
                if alpha is not None and _release_sort_key(alpha) > _release_sort_key(release): release = alpha
            except Exception:
                cached_prerelease = _cached_release(cache, True)
                if cached_prerelease is not None and _release_sort_key(cached_prerelease) > _release_sort_key(release):
                    release = cached_prerelease; release_source = "cached"; release_checked_at = cached_prerelease.checked_at
        checked_at = utc_now()
        cached_releases = dict(cache.get("releases", {})) if isinstance(cache.get("releases"), dict) else {}
        cached_releases["prerelease" if include_prereleases else "stable"] = asdict(_release_with_status(release, release_source, release_checked_at))
        _save_release_cache(cache_path, {**cache, "releases": cached_releases, "checkedAt": checked_at, "apiError": str(last_error)})
        return _release_with_status(release, release_source, release_checked_at)
    except Exception as feed_error:
        if cached := _cached_release(cache, include_prereleases): return cached
        raise RuntimeError(f"GitHub API and releases feed failed: {last_error}; {feed_error}") from feed_error

def development_roots(extra_roots: str = "", home: Path | None = None, environment: dict | None = None) -> list[str]:
    """Return normalized discovery roots: development folders, cloud storage, and configured extras."""
    home = home or Path.home(); environment = os.environ if environment is None else environment
    roots = [str(home / "Documents" / "NVDA Add-on Development" / "Add-ons"), str(home / "Documents" / "Codex")]
    for variable in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial", "Dropbox", "GoogleDrive", "iCloudDrive"):
        if value := environment.get(variable): roots.append(value)
    roots.extend(str(home / name) for name in ("OneDrive", "Dropbox", "Google Drive", "iCloudDrive"))
    roots.extend(value.strip() for value in str(extra_roots or "").split(";") if value.strip())
    return list(dict.fromkeys(os.path.normcase(os.path.expandvars(os.path.expanduser(root))) for root in roots))

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
        if (current / ".git").exists() or (current / "buildVars.py").is_file() or (current / "sconstruct").is_file(): return current.resolve()
        if current.parent == current: break
        current = current.parent
    return None

def project_id(path: Path) -> str:
    # Use the manifest path, not just the repository root: monorepos can contain several add-ons.
    manifest = path.resolve()
    return hashlib.sha256(os.path.normcase(str(manifest)).encode("utf-8")).hexdigest()[:16]

def approved_manifest_paths(state: dict) -> list[Path]:
    if not isinstance(state, dict): return []
    approved = set(state.get("approvedProjects", [])) if isinstance(state.get("approvedProjects"), list) else set(); ignored = ignored_project_ids(state); found = []
    projects = state.get("projects", []) if isinstance(state.get("projects"), list) else []
    for item in projects:
        if not isinstance(item, dict): continue
        try:
            path = Path(item["path"])
            old_identifier = item.get("project_id")
            if old_identifier in approved and old_identifier not in ignored and path.is_file() and not is_offline(path): found.append(path)
        except (KeyError, OSError, TypeError, ValueError):
            continue
    unique = {}
    for path in found:
        try:
            resolved = path.resolve(); unique[os.path.normcase(str(resolved))] = resolved
        except (OSError, RuntimeError): pass
    return list(unique.values())

def approved_project_ids(state: dict) -> set[str]:
    """Return current IDs while migrating approvals created by older ID algorithms."""
    approved = set(state.get("approvedProjects", [])) if isinstance(state, dict) and isinstance(state.get("approvedProjects"), list) else set()
    projects = state.get("projects", []) if isinstance(state, dict) and isinstance(state.get("projects"), list) else []
    for item in projects:
        if not isinstance(item, dict) or item.get("project_id") not in approved or not item.get("path"): continue
        try: approved.add(project_id(Path(item["path"])))
        except (OSError, TypeError, ValueError): pass
    return approved


HIDDEN_STATUS = "hidden by you"

def hidden_project_ids(state: dict) -> set[str]:
    """Return copies the user chose never to be offered again."""
    return set(state.get("hiddenProjects", [])) if isinstance(state, dict) and isinstance(state.get("hiddenProjects"), list) else set()

def project_result_from_record(item: dict) -> ProjectResult | None:
    """Rebuild a saved record, ignoring keys from other versions of the add-on."""
    fields = ProjectResult.__dataclass_fields__
    try: return ProjectResult(**{key: value for key, value in item.items() if key in fields})
    except TypeError: return None

def tracking_state(state: dict, identifier: str) -> str:
    if identifier in hidden_project_ids(state): return "hidden"
    return "tracked" if identifier in saved_approved_project_ids(state) else "untracked"

def set_tracking(state: dict, changes: dict[str, str]) -> None:
    """Stop tracking or hide copies, or unhide copies about to be tracked.

    Starting to track is completed by the validating approval step, which adds
    the copy to approvedProjects; this only removes it from the hidden list.
    """
    approved = saved_approved_project_ids(state); hidden = hidden_project_ids(state); released = set()
    for identifier, choice in changes.items():
        if choice == "hidden": approved.discard(identifier); hidden.add(identifier); released.add(identifier)
        elif choice == "untracked": approved.discard(identifier); hidden.discard(identifier); released.add(identifier)
        elif choice == "tracked": hidden.discard(identifier)
    for item in state.get("projects", []) if isinstance(state.get("projects"), list) else []:
        if isinstance(item, dict) and changes.get(item.get("project_id")) in {"hidden", "untracked"}:
            item["status"] = HIDDEN_STATUS if changes[item["project_id"]] == "hidden" else "awaiting manual approval"
    state["approvedProjects"] = sorted(approved); state["hiddenProjects"] = sorted(hidden)
    # A copy that is no longer tracked must not keep a pending compatibility update.
    if released: finish_manual_review_items(state, released)

def saved_approved_project_ids(state: dict) -> set[str]:
    """Return saved IDs without resolving paths; safe for NVDA's main thread."""
    return set(state.get("approvedProjects", [])) if isinstance(state, dict) and isinstance(state.get("approvedProjects"), list) else set()

def path_kind(path: str) -> str:
    normalized = os.path.normcase(os.path.abspath(path)).replace("/", "\\")
    if "\\documents\\nvda add-on development\\add-ons\\" in normalized: return "primary"
    if "\\documents\\codex\\" in normalized: return "codex"
    return "other"

def duplicate_codex_work_copy_ids(projects: list[dict]) -> set[str]:
    groups = {}
    for item in projects if isinstance(projects, list) else []:
        if not isinstance(item, dict) or not item.get("name") or not item.get("path") or not item.get("project_id"): continue
        groups.setdefault(str(item["name"]).casefold(), []).append(item)
    ignored = set()
    for items in groups.values():
        primary = [item for item in items if path_kind(item["path"]) == "primary"]
        if not primary: continue
        def local_version(item):
            return addon_version_key(item.get("addon_version", ""))
        known_primary = [item for item in primary if item.get("addon_version")]
        if not known_primary: continue
        newest_primary = max(local_version(item) for item in known_primary)
        ignored.update(item["project_id"] for item in items if path_kind(item["path"]) == "codex" and item.get("addon_version") and local_version(item) <= newest_primary)
    return ignored

def ignored_project_ids(state: dict) -> set[str]:
    projects = state.get("projects", []) if isinstance(state, dict) else []
    # Duplicate exclusions are derived data. Recompute them so an older saved choice
    # cannot hide a work copy whose manifest has since become the newest version.
    return duplicate_codex_work_copy_ids(projects)

def visible_project_records(state: dict) -> tuple[list[dict], set[str]]:
    projects = state.get("projects", []) if isinstance(state, dict) and isinstance(state.get("projects"), list) else []
    ignored = ignored_project_ids(state)
    return [item for item in projects if isinstance(item, dict) and item.get("project_id") not in ignored], ignored

def notification_state(records: list[dict], previous_updates: dict, previous_awaiting: dict, previous_failures: dict):
    """Return current notification maps, changed IDs, and resolved failure IDs."""
    valid = [item for item in records if isinstance(item, dict) and item.get("project_id")]
    updates = {item["project_id"]: str(item.get("status", "")) for item in valid if str(item.get("status", "")).startswith("update available: ")}
    awaiting = {item["project_id"]: str(item.get("status", "")) for item in valid if "awaiting" in str(item.get("status", ""))}
    failures = {item["project_id"]: str(item.get("status", "")) for item in valid if "failed" in str(item.get("status", ""))}
    changed = {identifier for mapping, previous in ((updates, previous_updates), (awaiting, previous_awaiting), (failures, previous_failures)) for identifier, status in mapping.items() if previous.get(identifier) != status}
    return updates, awaiting, failures, changed, set(previous_failures) - set(failures)

def addon_version_key(value: str) -> tuple:
    """Compare common add-on versions numerically, with stable builds after prereleases."""
    value = str(value or "").strip().lower().removeprefix("v")
    stage_match = re.search(r"(alpha|beta|dev|rc|a|b)(\d*)", value)
    base = value[:stage_match.start()].rstrip("._-") if stage_match else value
    numbers = tuple(int(part) for part in re.findall(r"\d+", base))
    if stage_match:
        stage = {"dev": 0, "alpha": 1, "a": 1, "beta": 2, "b": 2, "rc": 3}[stage_match.group(1)]
        stage_number = int(stage_match.group(2) or 0)
    else:
        stage, stage_number = 4, 0
    return numbers, stage, stage_number, value

def newest_local_project_records(projects: list[dict]) -> tuple[list[dict], list[dict]]:
    """Keep only the newest local manifest for each unique add-on name."""
    selected = {}; skipped = []
    path_priority = {"codex": 0, "other": 1, "primary": 2}
    for item in projects:
        if not isinstance(item, dict) or not item.get("path"): continue
        name = str(item.get("name") or "").strip()
        key = name.casefold() if name and item.get("addon_version") else str(item.get("project_id") or item["path"]).casefold()
        candidate = (addon_version_key(item.get("addon_version", "")), path_priority[path_kind(item["path"])], str(item["path"]).casefold())
        previous = selected.get(key)
        if previous is None or candidate > previous[0]:
            if previous is not None: skipped.append(previous[1])
            selected[key] = (candidate, item)
        else: skipped.append(item)
    kept = [entry[1] for entry in selected.values()]
    return sorted(kept, key=lambda item: (str(item.get("name", "")).casefold(), str(item.get("path", "")).casefold())), skipped

def merge_project_records(previous: list[dict], current: list[ProjectResult]) -> list[dict]:
    """Retain known projects when a bounded or root-specific scan does not encounter them."""
    if not isinstance(previous, list): previous = []
    merged = {os.path.normcase(str(item.get("path"))): dict(item) for item in previous if isinstance(item, dict) and item.get("project_id") and item.get("path")}
    for item in current:
        key = os.path.normcase(item.path); updated = dict(item.__dict__)
        if not updated.get("addon_version") and key in merged:
            updated["addon_version"] = merged[key].get("addon_version", "")
        merged[key] = updated
    return sorted(merged.values(), key=lambda item: (str(item.get("name", "")).lower(), str(item.get("path", "")).lower()))

def unavailable_manifest_results(paths: list[str], previous: list[dict]) -> list[ProjectResult]:
    """Turn worker-confirmed unavailable paths into current, non-stale project results."""
    previous_by_path = {os.path.normcase(str(item.get("path", ""))): item for item in previous if isinstance(item, dict) and item.get("path")}
    results = []
    for value in paths if isinstance(paths, list) else ():
        if not isinstance(value, str) or not value: continue
        path = Path(value); old = previous_by_path.get(os.path.normcase(value), {})
        results.append(ProjectResult(str(old.get("project_id") or project_id(path)), str(old.get("name") or path.parent.name), value, "validation failed: approved manifest path is missing or offline"))
    return results

def wait_for_worker(process, timeout: int = 90, terminate_timeout: int = 5) -> int:
    """Wait for the isolated worker without allowing a filesystem call to hang forever."""
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        process.terminate()
        try:
            process.wait(timeout=terminate_timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            try: process.wait(timeout=terminate_timeout)
            except subprocess.TimeoutExpired: pass
        raise RuntimeError(f"The isolated project-discovery worker exceeded its {timeout}-second safety limit") from error

def validate(path: Path, cancelled=lambda: False, check_python: bool = True) -> list[str]:
    errors = []; metadata = values(path); text = path.read_text(encoding="utf-8-sig")
    for key in ("name", "summary", "version", "minimumnvdaversion", "lasttestednvdaversion"):
        if not metadata.get(key): errors.append(f"missing {key}")
    for key in ("minimumnvdaversion", "lasttestednvdaversion"):
        if metadata.get(key):
            try: version_tuple(metadata[key])
            except ValueError as error: errors.append(str(error))
    if metadata.get("minimumnvdaversion") and metadata.get("lasttestednvdaversion"):
        try:
            if version_tuple(metadata["minimumnvdaversion"]) > version_tuple(metadata["lasttestednvdaversion"]): errors.append("minimumNVDAVersion is newer than lastTestedNVDAVersion")
        except ValueError: pass
    if len(re.findall(r"^[ \t]*lastTestedNVDAVersion[ \t]*=", text, re.I | re.M)) != 1: errors.append("lastTestedNVDAVersion must occur exactly once")
    if not check_python: return errors
    # Check syntax against the Python bundled with the oldest NVDA the manifest supports.
    try: target = python_compatibility.python_version_for_nvda(metadata.get("minimumnvdaversion", ""))
    except ValueError: target = python_compatibility.python_version_for_nvda("2026.1")
    checked = 0
    for directory, dirnames, filenames in os.walk(path.parent, topdown=True, onerror=lambda _error: None):
        if cancelled(): return ["cancelled"]
        dirnames[:] = [name for name in dirnames if not should_prune(name) and name != ".git"]
        for filename in filenames:
            if cancelled(): return ["cancelled"]
            if not filename.lower().endswith(".py"): continue
            checked += 1
            if checked > 5000: return errors + ["validation stopped after 5000 Python files"]
            if checked % 25 == 0: time.sleep(0.002)
            source = Path(directory) / filename
            try:
                if source.stat().st_size > MAX_PYTHON_FILE_BYTES:
                    errors.append(f"{source}: Python file exceeds the {MAX_PYTHON_FILE_BYTES // (1024 * 1024)} MB validation limit"); continue
                errors.extend(message[:1000] for message in python_compatibility.compatibility_errors(source.read_text(encoding="utf-8-sig"), str(source), target))
            except (UnicodeError, OSError, ValueError) as error: errors.append(f"{source}: {error}"[:1000])
            if len(errors) >= MAX_VALIDATION_ERRORS: return errors + [f"validation stopped after {MAX_VALIDATION_ERRORS} errors"]
    return errors

def git_manifest_state(path: Path) -> tuple[str, bool]:
    """Return the current branch label and whether this manifest is locally changed."""
    root = project_root(path)
    if root is None or not (root / ".git").exists():
        return "not a Git repository", False
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        branch_result = subprocess.run(
            ["git", "-C", str(root), "branch", "--show-current"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=flags,
        )
        if branch_result.returncode:
            raise RuntimeError(branch_result.stderr.strip() or "Git branch lookup failed")
        branch = branch_result.stdout.strip()
        if not branch:
            commit_result = subprocess.run(
                ["git", "-C", str(root), "rev-parse", "--short", "HEAD"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                creationflags=flags,
            )
            branch = f"detached at {commit_result.stdout.strip()}" if commit_result.returncode == 0 else "detached HEAD"
        relative = path.resolve().relative_to(root).as_posix()
        status_result = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain=v1", "--untracked-files=all", "--", relative],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            creationflags=flags,
        )
        if status_result.returncode:
            raise RuntimeError(status_result.stderr.strip() or "Git status lookup failed")
        return branch, bool(status_result.stdout.strip())
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError):
        return "Git status unavailable", False

def _git_output(root: Path, *arguments: str) -> tuple[int, str]:
    try:
        completed = subprocess.run(["git", "-C", str(root), *arguments], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return completed.returncode, completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return -1, ""

def add_copy_details(result: ProjectResult, path: Path) -> ProjectResult:
    """Record what tells this copy apart from other copies: public name, version, Git, and GitHub state.

    Runs Git, so call it only on a worker thread.  Every lookup is local; nothing
    contacts GitHub, so unpushed counts compare against the last fetched state.
    """
    try:
        metadata = values(path); result.summary = metadata.get("summary", ""); result.addon_version = metadata.get("version", result.addon_version)
        result.last_change = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).date().isoformat()
    except (OSError, UnicodeError, ValueError):
        pass
    root = project_root(path)
    if root is None or not (root / ".git").exists():
        result.branch = "not a Git repository"; result.remote = ""
        return result
    code, branch = _git_output(root, "branch", "--show-current")
    if code == 0: result.branch = branch or "detached"
    code, remotes = _git_output(root, "remote")
    names = [name for name in remotes.splitlines() if name.strip()] if code == 0 else []
    remote_name = "origin" if "origin" in names else (names[0] if names else "")
    if remote_name:
        code, url = _git_output(root, "remote", "get-url", remote_name)
        if code == 0: result.remote = url
    code, status = _git_output(root, "status", "--porcelain=v1")
    if code == 0: result.uncommitted_changes = len([line for line in status.splitlines() if line.strip()])
    code, ahead = _git_output(root, "rev-list", "--count", "@{upstream}..HEAD")
    if code == 0 and ahead.isdigit(): result.unpushed_commits = int(ahead)
    code, last = _git_output(root, "log", "-1", "--format=%cs")
    if code == 0 and re.fullmatch(r"\d{4}-\d{2}-\d{2}", last): result.last_change = last
    return result

def github_repository_label(remote: str) -> str:
    """Return owner/repository for a GitHub remote, or an empty string for any other remote."""
    match = re.match(r"^(?:https://github\.com/|git@github\.com:)([^/\s]+/[^/\s]+?)(?:\.git)?/?$", str(remote or "").strip(), re.I)
    return match.group(1) if match else ""

def _spoken_date(value: str) -> str:
    # Month names read naturally; 2026-09-14 is read as numbers separated by minus.
    try:
        day = datetime.strptime(value, "%Y-%m-%d"); return f"{day:%B} {day.day}, {day.year}"
    except (TypeError, ValueError):
        return ""

def copy_rank(version: str, versions: list[str], translate=lambda text: text) -> str:
    """Say whether a copy is the newest among copies of one add-on; empty when it is the only copy."""
    _ = translate; known = [item for item in versions if item and item != "unknown"]
    if len(versions) < 2 or not known: return ""
    newest = max(known, key=addon_version_key)
    return _("newest copy") if version == newest else _("older copy; the newest is %s") % newest

def describe_copy(result: ProjectResult, same_addon_versions: list[str], tracked_versions: list[str], home: Path | None = None, translate=lambda text: text) -> str:
    """Describe one found copy so that copies of the same add-on can be told apart by ear and in braille.

    The most distinguishing facts come first: name and version, whether this is the
    newest copy, whether another copy is already tracked, and where its code lives.
    """
    _ = translate; parts = []
    version = result.addon_version or _("unknown version")
    parts.append(f"{result.summary or result.name} {version}")
    # Tracked copies count when deciding which copy is newest.
    known = [item for item in [*same_addon_versions, *tracked_versions] if item and item != "unknown"]
    tracked = sorted(set(tracked_versions), key=addon_version_key)
    if version in tracked:
        parts.append(_("you already track another copy of this same version"))
    else:
        if len(same_addon_versions) + len(tracked_versions) > 1 and known:
            newest = max(known, key=addon_version_key)
            parts.append(_("newest copy") if version == newest else _("older copy; the newest is %s") % newest)
        if tracked:
            parts.append(_("you already track version %s") % tracked[0] if len(tracked) == 1 else _("you already track versions %s and %s") % (", ".join(tracked[:-1]), tracked[-1]))
    repository = github_repository_label(result.remote)
    if result.remote and repository:
        state = [_("on GitHub as %s") % repository]
        if result.uncommitted_changes > 0: state.append(_("1 file changed but not committed") if result.uncommitted_changes == 1 else _("%d files changed but not committed") % result.uncommitted_changes)
        if result.unpushed_commits > 0: state.append(_("1 commit not pushed") if result.unpushed_commits == 1 else _("%d commits not pushed") % result.unpushed_commits)
        if result.uncommitted_changes == 0 and result.unpushed_commits == 0: state.append(_("everything pushed"))
        elif result.uncommitted_changes == 0 and result.unpushed_commits < 0 and result.branch != "detached":
            # Without a linked GitHub branch, Git cannot tell whether this branch's commits were pushed.
            state.append(_("this branch is not linked to a GitHub branch"))
        parts.append(", ".join(state))
    elif result.remote:
        parts.append(_("not on GitHub; its Git remote is %s") % result.remote)
    elif result.uncommitted_changes < 0 and result.branch != "not a Git repository":
        # Git was not inspected for this copy, so make no claim about GitHub.
        parts.append(_("GitHub state not checked"))
    elif result.branch and result.branch != "not a Git repository":
        changed = "" if result.uncommitted_changes <= 0 else (_(", 1 file changed but not committed") if result.uncommitted_changes == 1 else _(", %d files changed but not committed") % result.uncommitted_changes)
        parts.append(_("only on this computer; no GitHub repository") + changed)
    else:
        parts.append(_("only on this computer; not in Git"))
    if result.branch and result.branch.casefold() not in {"main", "master", "not a git repository"}:
        parts.append(_("branch %s") % result.branch)
    if spoken := _spoken_date(result.last_change):
        parts.append(_("last changed %s") % spoken)
    folder = Path(result.path).parent
    try: folder = folder.relative_to(home or Path.home())
    except ValueError: pass
    parts.append(_("folder %s") % folder)
    return "; ".join(parts)

def _atomic_replace_bytes(path: Path, data: bytes) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

def _write_manifest_preserving_format(path: Path, changed_text: str) -> None:
    original = path.read_bytes(); has_bom = original.startswith(codecs.BOM_UTF8); output = changed_text.encode("utf-8")
    if has_bom: output = codecs.BOM_UTF8 + output
    _atomic_replace_bytes(path, output)

def _next_backup_path(folder: Path) -> Path:
    candidate = folder / "manifest.ini"; number = 2
    while candidate.exists():
        candidate = folder / f"manifest-{number}.ini"; number += 1
    return candidate

def update(path: Path, release: Release, backup_root: Path, apply_changes: bool = False, cancelled=lambda: False, before_change=None) -> ProjectResult:
    metadata = values(path); name = metadata.get("name", path.parent.name); identifier = project_id(path)
    if cancelled(): return ProjectResult(identifier, name, str(path), "cancelled")
    # Preview checks are deliberately manifest-only. Parsing entire source trees inside
    # NVDA can compete with its main thread even when performed on a Python thread.
    errors = validate(path, cancelled, check_python=False)
    if cancelled(): return ProjectResult(identifier, name, str(path), "cancelled")
    if errors: return ProjectResult(identifier, name, str(path), "validation failed: " + "; ".join(errors))
    current = metadata["lasttestednvdaversion"]
    if version_tuple(current) >= version_tuple(release.manifest_version): return ProjectResult(identifier, name, str(path), "current")
    if not apply_changes: return ProjectResult(identifier, name, str(path), f"update available: {current} to {release.manifest_version}")
    raw = path.read_bytes()
    original = raw[len(codecs.BOM_UTF8):].decode("utf-8") if raw.startswith(codecs.BOM_UTF8) else raw.decode("utf-8")
    match = LAST_TESTED.search(original)
    if not match: return ProjectResult(identifier, name, str(path), "validation failed: compatibility key not found")
    safe_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")[:80] or "add-on"
    safe_tag = re.sub(r"[^A-Za-z0-9._-]+", "_", release.tag).strip(".")[:80] or "release"
    if safe_tag.upper() in WINDOWS_RESERVED_NAMES: safe_tag = f"release-{safe_tag}"
    if before_change is not None: before_change(name, current, release.manifest_version)
    if cancelled(): return ProjectResult(identifier, name, str(path), "cancelled")
    backup_folder = backup_root / safe_tag / f"{safe_name}-{identifier}"; backup_folder.mkdir(parents=True, exist_ok=True)
    backup = _next_backup_path(backup_folder); shutil.copy2(path, backup)
    changed = original[:match.start()] + f"{match.group(1)}{match.group('quote')}{release.manifest_version}{match.group('quote')}{match.group('suffix')}{match.group('cr')}" + original[match.end():]
    _write_manifest_preserving_format(path, changed)
    try:
        errors = validate(path, check_python=False)
        target_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        _atomic_replace_bytes(path, raw)
        raise
    if errors:
        _atomic_replace_bytes(path, raw)
        return ProjectResult(identifier, name, str(path), "validation failed after the update; the original manifest was restored: " + "; ".join(errors))
    return ProjectResult(identifier, name, str(path), f"updated to {release.manifest_version}", str(backup), current, release.manifest_version, target_hash)

def downgrade(path: Path, target_version: str, backup_root: Path, official_versions: dict[str, bool], cancelled=lambda: False) -> ProjectResult:
    target_version = target_version.strip(); version_tuple(target_version)
    metadata = values(path); name = metadata.get("name", path.parent.name); identifier = project_id(path)
    if target_version not in official_versions: return ProjectResult(identifier, name, str(path), f"validation failed: {target_version} is not a recognized NVDA API version")
    if official_versions[target_version] and metadata.get("updatechannel", "").casefold() not in {"beta", "dev"}:
        return ProjectResult(identifier, name, str(path), f"validation failed: experimental NVDA target {target_version} requires updateChannel beta or dev")
    errors = validate(path, check_python=False)
    if errors: return ProjectResult(identifier, name, str(path), "validation failed: " + "; ".join(errors))
    current = metadata["lasttestednvdaversion"]
    if version_tuple(target_version) >= version_tuple(current): return ProjectResult(identifier, name, str(path), f"downgrade skipped: {target_version} is not older than {current}")
    minimum = metadata["minimumnvdaversion"]
    if version_tuple(target_version) < version_tuple(minimum): return ProjectResult(identifier, name, str(path), f"validation failed: {target_version} is older than minimumNVDAVersion {minimum}")
    raw = path.read_bytes()
    original = raw[len(codecs.BOM_UTF8):].decode("utf-8") if raw.startswith(codecs.BOM_UTF8) else raw.decode("utf-8")
    match = LAST_TESTED.search(original)
    if not match: return ProjectResult(identifier, name, str(path), "validation failed: compatibility key not found")
    if cancelled(): return ProjectResult(identifier, name, str(path), "cancelled")
    safe_name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(" .")[:80] or "add-on"; safe_target = re.sub(r"[^A-Za-z0-9._-]+", "_", target_version).strip(".")[:80] or "target"
    backup_folder = backup_root / f"compatibility-{safe_target}" / f"{safe_name}-{identifier}"; backup_folder.mkdir(parents=True, exist_ok=True)
    backup = _next_backup_path(backup_folder); shutil.copy2(path, backup)
    changed = original[:match.start()] + f"{match.group(1)}{match.group('quote')}{target_version}{match.group('quote')}{match.group('suffix')}{match.group('cr')}" + original[match.end():]
    if cancelled(): return ProjectResult(identifier, name, str(path), "cancelled")
    _write_manifest_preserving_format(path, changed)
    try:
        errors = validate(path, check_python=False)
        target_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    except Exception:
        _atomic_replace_bytes(path, raw)
        raise
    if errors:
        _atomic_replace_bytes(path, raw)
        return ProjectResult(identifier, name, str(path), "validation failed after the change; the original manifest was restored: " + "; ".join(errors))
    return ProjectResult(identifier, name, str(path), f"set last tested NVDA from {current} to {target_version}", str(backup), current, target_version, target_hash)

def undo_compatibility_target(path: Path, backup_path: Path, expected_target: str, expected_hash: str, backup_root: Path) -> ProjectResult:
    """Restore a recorded compatibility backup without overwriting later edits."""
    metadata = values(path); name = metadata.get("name", path.parent.name); identifier = project_id(path)
    try:
        backup = backup_path.resolve(strict=True)
        backup.relative_to(backup_root.resolve(strict=True))
    except (OSError, RuntimeError, ValueError):
        return ProjectResult(identifier, name, str(path), "undo failed: the recorded backup is missing or outside the updater backup folder")
    current = metadata.get("lasttestednvdaversion", "")
    if current != expected_target:
        return ProjectResult(identifier, name, str(path), f"undo failed: lastTestedNVDAVersion is now {current or 'missing'}, expected {expected_target}; no later edits were overwritten")
    if not expected_hash or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
        return ProjectResult(identifier, name, str(path), "undo failed: the manifest changed after the compatibility operation; no later edits were overwritten")
    try:
        backup_metadata = values(backup)
        previous = backup_metadata["lasttestednvdaversion"]
        if backup_metadata.get("name", "").casefold() != metadata.get("name", "").casefold():
            raise ValueError("backup add-on name does not match")
        current_bytes = path.read_bytes()
        _atomic_replace_bytes(path, backup.read_bytes())
        errors = validate(path, check_python=False)
        if errors:
            _atomic_replace_bytes(path, current_bytes)
            return ProjectResult(identifier, name, str(path), "undo failed validation; the newer manifest was restored: " + "; ".join(errors))
        return ProjectResult(identifier, name, str(path), f"restored last tested NVDA from {current} to {previous}", "", current, previous)
    except (KeyError, OSError, UnicodeError, ValueError) as error:
        return ProjectResult(identifier, name, str(path), f"undo failed: {error}")
