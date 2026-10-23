from __future__ import annotations

import base64, hashlib, io, json, os, re, shutil, stat, subprocess, tempfile, threading, time, urllib.parse, urllib.request, webbrowser, zipfile
from dataclasses import dataclass
from pathlib import Path

try:
    from . import python_compatibility
except ImportError:
    # Unit tests and the build tool import these modules directly from their folder.
    import python_compatibility

try:
    from ._vendor import yaml
except ImportError:
    # Unit tests also import this module directly from its folder.
    from _vendor import yaml

class _GitHubYamlLoader(yaml.SafeLoader):
    """Safe YAML loader whose booleans match GitHub's true/false form syntax."""

_GitHubYamlLoader.yaml_implicit_resolvers = {
    key: [resolver for resolver in value if resolver[0] != "tag:yaml.org,2002:bool"]
    for key, value in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_GitHubYamlLoader.add_implicit_resolver("tag:yaml.org,2002:bool", re.compile(r"^(?:true|false)$", re.I), list("tTfF"))

STORE_FORM = "https://github.com/nvaccess/addon-datastore/issues/new?template=registerAddon.yml"
LICENSE_FILE_NAMES = ("copying.txt", "copying", "copying.md", "license", "license.txt", "license.md")
# Each signature matches the heading of a standard license text.  The earliest
# match wins, so a GPL text that mentions another license later is unaffected.
_LICENSE_SIGNATURES = (
    (re.compile(r"GNU LESSER GENERAL PUBLIC LICENSE\s+Version 3", re.I), "LGPL v3", "https://www.gnu.org/licenses/lgpl-3.0.html"),
    (re.compile(r"GNU LESSER GENERAL PUBLIC LICENSE\s+Version 2\.1", re.I), "LGPL v2.1", "https://www.gnu.org/licenses/old-licenses/lgpl-2.1.html"),
    (re.compile(r"GNU GENERAL PUBLIC LICENSE\s+Version 3", re.I), "GPL v3", "https://www.gnu.org/licenses/gpl-3.0.html"),
    (re.compile(r"GNU GENERAL PUBLIC LICENSE\s+Version 2", re.I), "GPL v2", "https://www.gnu.org/licenses/gpl-2.0.html"),
    (re.compile(r"Apache License\s+Version 2\.0", re.I), "Apache 2.0", "https://www.apache.org/licenses/LICENSE-2.0"),
    (re.compile(r"Mozilla Public License,?\s+(?:Version|v\.)\s*2\.0", re.I), "MPL 2.0", "https://www.mozilla.org/MPL/2.0/"),
    (re.compile(r"\bMIT License\b|Permission is hereby granted, free of charge", re.I), "MIT", "https://opensource.org/license/mit"),
)
CHANGELOG_FILE_NAMES = ("changelog.md", "changes.md", "history.md")
MAX_RELEASE_NOTES_CHARACTERS = 20000
GITHUB_DEVICE_URL = "https://github.com/login/device"
GITHUB_CLI_URL = "https://cli.github.com/"
NVDA_API_VERSIONS_URL = "https://raw.githubusercontent.com/nvaccess/addon-datastore/master/transform/nvdaAPIVersions.json"
RUNTIME_NAMES = {"appmodules", "brailledisplaydrivers", "copying.txt", "doc", "globalplugins", "installtasks.py", "license.txt", "locale", "manifest.ini", "synthdrivers"}
SENSITIVE_NAMES = {".env", "credentials.json", "id_dsa", "id_ed25519", "id_rsa", "secrets.json"}
SENSITIVE_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
GENERATED_DIRECTORIES = {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", "build", "build-local", "dist", "outputs"}
GENERATED_SUFFIXES = {".pyc", ".pyo"}
MAX_PACKAGE_BYTES = 100 * 1024 * 1024
MAX_EXPANDED_PACKAGE_BYTES = 512 * 1024 * 1024
_AI_DISCLOSURE_PATTERNS = (
    # The bare acronym is case-sensitive: lowercase "ai" is an ordinary word in
    # Italian, Portuguese, and other translations shipped in locale files.
    re.compile(r"\b" + "A" + "I" + r"\b"),
    re.compile(r"\bAI(?:[- ]generated|[- ]written|[- ]authored)\b", re.I),
    re.compile(r"\b(?:generated|written|authored|created)\s+(?:by|with|using)\s+(?:an?\s+)?(?:" + "A" + "I" + r"|ChatGPT|OpenAI|Codex|Copilot)\b", re.I),
    re.compile(r"\b(?:" + "A" + "I" + r" agent|artificial " + "intelligence" + r"|large language " + "model" + r"|LLM-" + "generated" + r")\b", re.I),
    re.compile(r"^\s*co-authored-by:.*(?:ChatGPT|OpenAI|Codex|Copilot)", re.I),
)

class AuthenticationRequired(RuntimeError):
    pass

class GitHubCliRequired(RuntimeError):
    pass

class OperationCancelled(RuntimeError):
    """Raised between projects when the user cancels; completed lists finished items."""
    def __init__(self, completed=()):
        super().__init__("the operation was cancelled")
        self.completed = list(completed)

def _stop_if_cancelled(cancelled, completed=()) -> None:
    # Cancellation is honored only between projects, never during a push or upload.
    if cancelled is not None and cancelled():
        raise OperationCancelled(completed)


_PROCESS_LOCK = threading.Lock()
_ACTIVE_PROCESSES = set()


def _track_process(process) -> None:
    with _PROCESS_LOCK:
        _ACTIVE_PROCESSES.add(process)


def _untrack_process(process) -> None:
    with _PROCESS_LOCK:
        _ACTIVE_PROCESSES.discard(process)


def _terminate_process(process, timeout: float = 1.0) -> None:
    try:
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            try: process.wait(timeout=timeout)
            except subprocess.TimeoutExpired: pass
    except OSError:
        pass


def cancel_running_commands() -> None:
    """Stop GitHub and project-builder commands when the add-on terminates."""
    with _PROCESS_LOCK:
        processes = list(_ACTIVE_PROCESSES)
    for process in processes:
        _terminate_process(process)

@dataclass
class ProjectPublishInfo:
    project_id: str
    name: str
    manifest: str
    root: str
    version: str
    summary: str
    publisher: str
    remote: str
    changed_files: int
    has_git: bool
    unpushed_commits: int
    github_release_version: str = ""
    github_release_download_url: str = ""
    repository_owned_by_user: bool = True
    release_package_verified: bool = True
    release_package_error: str = ""
    store_guideline_issues: tuple[str, ...] = ()
    channel: str = ""
    default_branch: str = ""
    current_branch: str = ""
    matching_release_tag: str = ""
    matching_release_download_url: str = ""
    matching_release_package_verified: bool = True
    matching_release_package_error: str = ""
    matching_release_targets_head: bool = True
    unpushed_check_error: str = ""
    source_fingerprint: str = ""
    license_name: str = ""
    license_url: str = ""
    release_notes: str = ""

@dataclass(frozen=True)
class GitHubAddonRepository:
    name: str
    full_name: str
    url: str
    description: str = ""
    private: bool = False
    archived: bool = False
    release_tag: str = ""
    prerelease: bool = False
    asset_name: str = ""
    download_url: str = ""
    asset_size: int = 0
    default_branch: str = ""
    issues_enabled: bool = True

@dataclass(frozen=True)
class GitHubIssueTemplate:
    path: str
    name: str
    sha: str = ""
    content: str = ""

@dataclass
class IssueTemplateDocument:
    """A parsed GitHub issue form or legacy Markdown issue template."""
    kind: str
    data: dict
    markdown_body: str = ""

SUPPORTED_ISSUE_FORM_TYPES = frozenset({"input", "textarea", "dropdown", "checkboxes", "markdown"})

_ADDON_REPOSITORIES_QUERY = r"""
query($endCursor: String) {
  viewer {
    login
    repositories(first: 100, after: $endCursor, ownerAffiliations: OWNER, orderBy: {field: NAME, direction: ASC}) {
      nodes {
        name
        nameWithOwner
        url
        description
        isPrivate
        isArchived
        releases(first: 20, orderBy: {field: CREATED_AT, direction: DESC}) {
          nodes {
            tagName
            isDraft
            isPrerelease
            releaseAssets(first: 100) {
              nodes { name downloadUrl size }
            }
          }
        }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""

_ADDON_PROJECTS_QUERY = r"""
query($endCursor: String) {
  viewer {
    login
    repositories(first: 100, after: $endCursor, ownerAffiliations: OWNER, orderBy: {field: NAME, direction: ASC}) {
      nodes {
        name
        nameWithOwner
        url
        description
        isPrivate
        isArchived
        hasIssuesEnabled
        defaultBranchRef { name }
        manifest: object(expression: "HEAD:manifest.ini") { __typename }
        addonManifest: object(expression: "HEAD:addon/manifest.ini") { __typename }
        nvdaManifest: object(expression: "HEAD:nvda/manifest.ini") { __typename }
        sourceManifest: object(expression: "HEAD:src/manifest.ini") { __typename }
        manifestTemplate: object(expression: "HEAD:manifest.ini.tpl") { __typename }
        addonManifestTemplate: object(expression: "HEAD:addon/manifest.ini.tpl") { __typename }
        buildVariables: object(expression: "HEAD:buildVars.py") { __typename }
      }
      pageInfo { hasNextPage endCursor }
    }
  }
}
"""
_ADDON_REPOSITORY_MARKERS = ("manifest", "addonManifest", "nvdaManifest", "sourceManifest", "manifestTemplate", "addonManifestTemplate", "buildVariables")

def store_readiness_reasons(report: ProjectPublishInfo) -> list[str]:
    """Explain every condition that prevents this local project being submitted."""
    reasons = []
    if not report.remote: reasons.append("no GitHub repository")
    elif not report.repository_owned_by_user: reasons.append("repository is not owned by the signed-in GitHub account")
    item = store_release_item(report)
    latest_matches = bool(report.github_release_version) and not release_needed(report.version, report.github_release_version)
    exact_release = bool(report.matching_release_tag) or latest_matches
    exact_verified = report.matching_release_package_verified if report.matching_release_tag else report.release_package_verified
    exact_error = report.matching_release_package_error if report.matching_release_tag else report.release_package_error
    if report.remote and report.repository_owned_by_user and not valid_store_release(item, report.version):
        if not report.github_release_version and not report.matching_release_tag: reasons.append("no GitHub Release")
        elif not exact_release: reasons.append(f"GitHub Release {report.github_release_version} does not match local version {report.version}")
        elif not item["downloadUrl"]: reasons.append("matching release has no .nvda-addon asset")
    if exact_release and item["downloadUrl"] and report.repository_owned_by_user and not exact_verified:
        reasons.append(exact_error or "release package manifest could not be verified")
    if report.changed_files: reasons.append(f"{report.changed_files} local changed {'file is' if report.changed_files == 1 else 'files are'} not included in the release")
    if report.unpushed_commits: reasons.append(f"{report.unpushed_commits} local commits are not pushed")
    if report.unpushed_check_error: reasons.append(report.unpushed_check_error)
    if report.matching_release_tag and not report.matching_release_targets_head:
        reasons.append(f"GitHub Release {report.matching_release_tag} points to an older commit; increment the manifest version before releasing the current code")
    reasons.extend(report.store_guideline_issues)
    return reasons


def store_release_item(report: ProjectPublishInfo) -> dict:
    """Build store data from the exact local-version release, never merely the newest release."""
    latest_matches = bool(report.github_release_version) and not release_needed(report.version, report.github_release_version)
    download = report.matching_release_download_url or (report.github_release_download_url if latest_matches else "")
    return {
        **report.__dict__,
        "sourceUrl": repository_url(report.remote) if report.remote else "",
        "downloadUrl": download,
    }

def _manifest_guideline_issues(fields: dict, api_versions: dict, submission_channel="") -> list[str]:
    issues = []
    name = fields.get("name", "")
    version = fields.get("version", "")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name): issues.append("manifest name must contain only letters, numbers, underscores, or hyphens")
    if not re.fullmatch(r"\d+\.\d+(?:\.\d+)?", version): issues.append("manifest version must be major.minor or major.minor.patch")
    for key in ("url",):
        value = fields.get(key, "")
        if value and not value.startswith("https://"): issues.append(f"manifest {key} must use HTTPS")
    known = set(api_versions)
    for key in ("minimumnvdaversion", "lasttestednvdaversion"):
        value = fields.get(key, "")
        if known and value not in known: issues.append(f"manifest {key} {value or 'is missing'} is not a current NVDA API version")
    last_tested = fields.get("lasttestednvdaversion", "")
    api = api_versions.get(last_tested, {}) if isinstance(api_versions, dict) else {}
    channel = (submission_channel or fields.get("updatechannel", "")).casefold()
    if isinstance(api, dict) and api.get("experimental") and channel not in {"beta", "dev"}:
        issues.append("experimental NVDA API versions require the beta or dev channel")
    return issues

def _ai_disclosure_issues(archive: zipfile.ZipFile) -> list[str]:
    """Inspect every text file shipped to users for automated-authorship disclosure wording."""
    findings = []
    text_suffixes = {".py", ".pyw", ".ini", ".md", ".txt", ".html", ".htm", ".json", ".yaml", ".yml", ".po", ".pot"}
    for info in archive.infolist():
        if info.is_dir() or info.file_size > 2 * 1024 * 1024 or Path(info.filename).suffix.casefold() not in text_suffixes: continue
        try: text = archive.read(info).decode("utf-8-sig")
        except (UnicodeError, OSError, RuntimeError): continue
        for line_number, line in enumerate(text.splitlines(), 1):
            if any(pattern.search(line) for pattern in _AI_DISCLOSURE_PATTERNS):
                findings.append(f"Automated-authorship wording in release package: {info.filename}, line {line_number}")
                if len(findings) >= 20: return findings
    return findings

def _source_disclosure_issues(root: Path, has_git: bool) -> list[str]:
    """Inspect source files that would be published, including repository-only files."""
    text_suffixes = {".py", ".pyw", ".ps1", ".ini", ".md", ".txt", ".html", ".htm", ".json", ".yaml", ".yml", ".po", ".pot"}
    if has_git:
        relative_names = [name for name in _git(root, "ls-files", "-co", "--exclude-standard", "-z").split("\0") if name]
        paths = [root / name for name in relative_names]
    else:
        paths = [path for path in root.rglob("*") if path.is_file()]
    findings = []
    for path in paths:
        try:
            if path.suffix.casefold() not in text_suffixes or path.stat().st_size > 2 * 1024 * 1024: continue
            text = path.read_text(encoding="utf-8-sig")
            relative = path.relative_to(root).as_posix()
        except (OSError, UnicodeError, ValueError): continue
        # Test data and the validator implementation necessarily spell out the
        # phrases being rejected.  They are controls, not authorship claims.
        # The shipped-package audit still checks every user-facing text file.
        parts = Path(relative).parts
        if (parts and parts[0].casefold() in {"test", "tests"}) or relative.casefold().endswith("tools/addon_store_metadata.py"):
            continue
        for line_number, line in enumerate(text.splitlines(), 1):
            if any(pattern.search(line) for pattern in _AI_DISCLOSURE_PATTERNS):
                findings.append(f"Automated-authorship wording in source: {relative}, line {line_number}")
                if len(findings) >= 20: return findings
    return findings

def _public_text_disclosure_issues(text: str, context: str) -> list[str]:
    findings = []
    for line_number, line in enumerate((text or "").splitlines(), 1):
        if any(pattern.search(line) for pattern in _AI_DISCLOSURE_PATTERNS):
            findings.append(f"Automated-authorship wording in {context}, line {line_number}")
    return findings

def _github_disclosure_issues(gh: str, remote: str, release_tag: str) -> list[str]:
    """Audit public submission-related GitHub text before opening the Store form."""
    repository = _remote_web_url(remote)
    findings = []
    details = json.loads(_run([gh, "repo", "view", repository, "--json", "description"]) or "{}")
    findings.extend(_public_text_disclosure_issues(details.get("description", ""), "GitHub repository description"))
    if release_tag:
        release = json.loads(_run([gh, "release", "view", release_tag, "--repo", repository, "--json", "name,body"]) or "{}")
        findings.extend(_public_text_disclosure_issues(release.get("name", ""), f"GitHub Release {release_tag} title"))
        findings.extend(_public_text_disclosure_issues(release.get("body", ""), f"GitHub Release {release_tag} notes"))
    pull_requests = json.loads(_run([gh, "pr", "list", "--repo", repository, "--state", "all", "--limit", "1000", "--json", "number,title,body"]) or "[]")
    for pull_request in pull_requests:
        number = pull_request.get("number", "unknown")
        findings.extend(_public_text_disclosure_issues(pull_request.get("title", ""), f"GitHub pull request {number} title"))
        findings.extend(_public_text_disclosure_issues(pull_request.get("body", ""), f"GitHub pull request {number} description"))
        if len(findings) >= 20:
            return findings[:20]
    return findings

def _parse_nvda_api_versions(data) -> dict:
    versions = {}
    for item in data if isinstance(data, list) else []:
        api = item.get("apiVer", {}) if isinstance(item, dict) else {}
        try:
            major, minor, patch = int(api["major"]), int(api["minor"]), int(api.get("patch", 0))
        except (KeyError, TypeError, ValueError):
            continue
        details = {"experimental": bool(item.get("experimental"))}
        versions[f"{major}.{minor}.{patch}"] = details
        if patch == 0: versions[f"{major}.{minor}"] = details
    return versions

def nvda_api_versions(cache_path: Path | None = None) -> dict:
    """Load official Store API versions, optionally falling back to a saved response."""
    request = urllib.request.Request(NVDA_API_VERSIONS_URL, headers={"User-Agent": "NVDA-Addon-Developer-Updater"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            data = json.load(response)
        versions = _parse_nvda_api_versions(data)
        if not versions: raise ValueError("the official response contained no NVDA API versions")
        if cache_path is not None:
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True); fd, temporary = tempfile.mkstemp(prefix=cache_path.name + ".", dir=cache_path.parent)
                try:
                    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream: json.dump(data, stream); stream.write("\n")
                    os.replace(temporary, cache_path)
                finally:
                    if os.path.exists(temporary): os.unlink(temporary)
            except OSError:
                # A read-only or full configuration folder must not discard a
                # valid response that has already been received from NV Access.
                pass
        return versions
    except Exception:
        if cache_path is None: raise
        try:
            with cache_path.open(encoding="utf-8-sig") as stream: versions = _parse_nvda_api_versions(json.load(stream))
            if versions: return versions
        except (OSError, TypeError, ValueError):
            pass
        raise

def normalized_version(value: str) -> str:
    normalized = (value or "").strip().removeprefix("v").removeprefix("V")
    return normalized[8:] if normalized.casefold().startswith("release-") else normalized

def release_needed(local_version: str, github_version: str) -> bool:
    return normalized_version(local_version).lower() != normalized_version(github_version).lower()

def matching_release_tag(tags: list[str], version: str) -> str:
    """Return an existing release tag for a version, regardless of tag prefix."""
    expected = normalized_version(version).casefold()
    return next((tag for tag in tags if normalized_version(tag).casefold() == expected), "")

def release_publish_action(item) -> str:
    """Return create, repair, or an empty string for the local version's release."""
    value = item.get if isinstance(item, dict) else lambda key, default=None: getattr(item, key, default)
    local_version = str(value("version", "") or "")
    latest_version = str(value("github_release_version", "") or "")
    explicit_matching_tag = str(value("matching_release_tag", "") or "")
    tag = explicit_matching_tag
    same_as_latest = bool(latest_version) and not release_needed(local_version, latest_version)
    if not tag and same_as_latest:
        tag = latest_version
    if not tag:
        return "create"
    if not bool(value("matching_release_targets_head", True)) or int(value("changed_files", 0) or 0):
        return "version-conflict"
    download = str(value("matching_release_download_url", "") or "")
    if not download and same_as_latest and not explicit_matching_tag:
        download = str(value("github_release_download_url", "") or "")
    verified = bool(value("matching_release_package_verified", True))
    if same_as_latest and not value("matching_release_tag", ""):
        verified = bool(value("release_package_verified", True))
    return "" if download and verified else "repair"

def public_name(item) -> str:
    """Return the human-facing add-on name without changing its package ID."""
    value = item.get if isinstance(item, dict) else lambda key, default=None: getattr(item, key, default)
    return str(value("summary", "") or value("name", "") or "add-on").strip()

def store_publisher_name(author: str) -> str:
    """Return a manifest author without e-mail addresses for the Store form."""
    without_addresses = re.sub(r"\s*<[^>]*>", "", str(author or ""))
    return " ".join(without_addresses.split()).strip(" ,")

def _project_folders(root: Path, manifest_folder: Path) -> list[Path]:
    folders = []
    for folder in (manifest_folder, root):
        if all(os.path.normcase(str(folder)) != os.path.normcase(str(existing)) for existing in folders):
            folders.append(folder)
    return folders

def _named_files(folder: Path, names: tuple[str, ...]) -> list[Path]:
    try:
        children = {child.name.casefold(): child for child in folder.iterdir() if child.is_file()}
    except OSError:
        return []
    return [children[name] for name in names if name in children]

def detect_license(root: Path, manifest_folder: Path) -> tuple[str, str]:
    """Identify a standard license from the project's license file, or return blanks."""
    for folder in _project_folders(root, manifest_folder):
        for path in _named_files(folder, LICENSE_FILE_NAMES):
            try:
                if path.stat().st_size > 1024 * 1024: continue
                text = path.read_text(encoding="utf-8-sig", errors="replace")[:20000]
            except OSError:
                continue
            matches = [(match.start(), name, url) for pattern, name, url in _LICENSE_SIGNATURES if (match := pattern.search(text))]
            if matches:
                _start, name, url = min(matches)
                return name, url
    return "", ""

def _version_pattern(version: str) -> str:
    return r"(?<![\w.])v?" + re.escape(normalized_version(version)) + r"(?![\w]|\.\d)"

def _changelog_section(text: str, version: str) -> str:
    headings = list(re.finditer(r"^(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*$", text, re.M))
    for index, heading in enumerate(headings):
        if not re.search(_version_pattern(version), heading.group(2), re.I):
            continue
        level = len(heading.group(1)); end = len(text)
        for later in headings[index + 1:]:
            if len(later.group(1)) <= level:
                end = later.start(); break
        return text[heading.end():end].strip()
    return ""

def release_notes(root: Path, manifest_folder: Path, version: str, manifest_changelog: str = "") -> str:
    """Return reviewed local notes for a version, or an empty string for the fixed notes.

    A project's changelog file is preferred.  The manifest changelog is used only
    when no changelog file exists, because a changelog file without this version
    means the manifest text probably describes an earlier release.
    """
    changelog_files = [path for folder in _project_folders(root, manifest_folder) for path in _named_files(folder, CHANGELOG_FILE_NAMES)]
    notes = ""
    for path in changelog_files:
        try: notes = _changelog_section(path.read_text(encoding="utf-8-sig", errors="replace"), version)
        except OSError: continue
        if notes: break
    if not changelog_files:
        notes = str(manifest_changelog or "").strip()
    if not notes or _public_text_disclosure_issues(notes, "release notes"):
        return ""
    if len(notes) > MAX_RELEASE_NOTES_CHARACTERS:
        notes = notes[:MAX_RELEASE_NOTES_CHARACTERS].rsplit("\n", 1)[0] + "\n\n(Notes shortened; see the project changelog for the complete list.)"
    return notes

def next_patch_version(version: str) -> str:
    """Return the next patch version for a numeric version, or an empty string."""
    match = re.fullmatch(r"(\d+)\.(\d+)(?:\.(\d+))?", str(version or "").strip())
    if not match: return ""
    major, minor, patch = match.groups()
    return f"{major}.{minor}.{int(patch) + 1}" if patch is not None else f"{major}.{minor}.1"

def _read_text_preserving_format(path: Path) -> tuple[bytes, str, bool]:
    raw = path.read_bytes(); has_bom = raw.startswith(b"\xef\xbb\xbf")
    return raw, raw[3:].decode("utf-8") if has_bom else raw.decode("utf-8"), has_bom

def _atomic_write_bytes(path: Path, data: bytes) -> None:
    mode = stat.S_IMODE(path.stat().st_mode)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream: stream.write(data)
        os.chmod(temporary, mode); os.replace(temporary, path)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)

def increment_project_version(manifest: Path, root: Path, current: str, new: str) -> list[str]:
    """Change an add-on version in its manifest, buildVars.py, and readme version lines.

    Every file is checked before anything is written, and written files are
    restored if a later write fails.  Returns the changed paths.
    """
    if not next_patch_version(current) or not re.fullmatch(r"\d+\.\d+\.\d+", new or ""):
        raise RuntimeError(f"version {current} cannot be incremented automatically")
    planned = {}
    raw, text, has_bom = _read_text_preserving_format(manifest)
    manifest_line = re.compile(r"^([ \t]*version[ \t]*=[ \t]*)([\"']?)" + re.escape(current) + r"\2([ \t]*(?:[#;].*)?)(\r?)$", re.I | re.M)
    if len(manifest_line.findall(text)) != 1:
        raise RuntimeError(f"{manifest} does not contain exactly one version = {current} line")
    planned[manifest] = (raw, manifest_line.sub(lambda match: f"{match.group(1)}{match.group(2)}{new}{match.group(2)}{match.group(3)}{match.group(4)}", text), has_bom)
    for folder in _project_folders(root, manifest.parent):
        build_variables = folder / "buildVars.py"
        if not build_variables.is_file(): continue
        raw, text, has_bom = _read_text_preserving_format(build_variables)
        if not re.search(r"[\"']addon_version[\"']", text): continue
        entry = re.compile(r"([\"']addon_version[\"']\s*:\s*)([\"'])" + re.escape(current) + r"\2")
        if len(entry.findall(text)) != 1:
            raise RuntimeError(f"{build_variables} defines addon_version in a form that cannot be changed safely; update the version manually")
        planned[build_variables] = (raw, entry.sub(lambda match: f"{match.group(1)}{match.group(2)}{new}{match.group(2)}", text), has_bom)
    documentation = manifest.parent / "doc"
    for path in sorted(documentation.glob("*/readme.*")) if documentation.is_dir() else []:
        if path.suffix.casefold() not in {".html", ".htm", ".md", ".txt"}: continue
        raw, text, has_bom = _read_text_preserving_format(path)
        if f"Version {current}." in text:
            planned[path] = (raw, text.replace(f"Version {current}.", f"Version {new}."), has_bom)
    written = []
    try:
        for path, (_raw, changed, has_bom) in planned.items():
            _atomic_write_bytes(path, (b"\xef\xbb\xbf" if has_bom else b"") + changed.encode("utf-8")); written.append(path)
    except Exception:
        for path in written:
            try: _atomic_write_bytes(path, planned[path][0])
            except OSError: pass
        raise
    return [str(path) for path in written]

def publish_plan(reports: list) -> tuple[list, list, list]:
    """Split preflight reports into publishable, not-owned, and version-conflict groups."""
    owned = [report for report in reports if getattr(report, "repository_owned_by_user", True)]
    not_owned = [report for report in reports if not getattr(report, "repository_owned_by_user", True)]
    conflicts = [report for report in owned if release_publish_action(report) == "version-conflict"]
    eligible = [report for report in owned if release_publish_action(report) != "version-conflict" and (report.changed_files or report.unpushed_commits or not report.remote or release_publish_action(report))]
    return eligible, not_owned, conflicts

def reports_with_current_releases(reports: list) -> list:
    """Return owned reports whose newest GitHub Release already matches the local version."""
    return [report for report in reports if getattr(report, "repository_owned_by_user", True) and not release_needed(getattr(report, "version", ""), getattr(report, "github_release_version", "")) and getattr(report, "github_release_download_url", "") and getattr(report, "remote", "")]

def reports_without_release_notes(reports: list) -> list:
    """Return reports whose new release would fall back to the one-line notes."""
    return [report for report in reports if release_publish_action(report) == "create" and not getattr(report, "release_notes", "")]

def resolve_release_channels(items: list[dict], saved_channels: dict) -> None:
    """Keep each preflight channel, using saved update channels only when one is missing."""
    saved = saved_channels if isinstance(saved_channels, dict) else {}
    for item in items:
        item["channel"] = item.get("channel") or saved.get(item.get("project_id"), "stable")

def _repository_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "nvda-addon"

def _manifest_github_remote(metadata: dict) -> str:
    for key in ("url", "sourceurl", "sourcecodeurl", "repository"):
        value = str(metadata.get(key, "")).strip()
        if value.startswith("https://github.com/") or value.startswith("git@github.com:"): return value
    return ""

def _command_failure_detail(stdout: str | None, stderr: str | None, limit: int = 1800) -> str:
    """Keep useful, bounded tails from both output streams."""
    streams = []
    for label, value in (("Output", stdout), ("Error output", stderr)):
        if not value or not value.strip():
            continue
        useful = "\n".join(
            line for line in value.strip().splitlines()
            if not re.search(r"\s\.\.\.\s+ok\s*$", line)
        ).strip()
        streams.append((label, useful or value.strip()))
    if not streams:
        return "the command returned no error details"
    stream_limit = max(200, (limit - (40 * len(streams))) // len(streams))
    excerpts = []
    for label, value in streams:
        if len(value) > stream_limit:
            value = "... earlier command output omitted ...\n" + value[-stream_limit:]
        excerpts.append(f"{label}:\n{value}")
    return "\n".join(excerpts)


# Quick Git and GitHub queries keep a short limit; transfers and project builders
# (which may run complete test suites) get longer limits so they are not killed midway.
COMMAND_TIMEOUT_SECONDS = 180
TRANSFER_TIMEOUT_SECONDS = 15 * 60
BUILDER_TIMEOUT_SECONDS = 30 * 60

def _timeout_text(seconds: int) -> str:
    return f"{seconds // 60}-minute" if seconds % 60 == 0 and seconds >= 600 else f"{seconds}-second"

def _run(arguments: list[str], cwd: Path | None = None, input_text: str | None = None, timeout: int = COMMAND_TIMEOUT_SECONDS) -> str:
    process = subprocess.Popen(arguments, cwd=cwd, stdin=subprocess.PIPE if input_text is not None else None, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    _track_process(process)
    try:
        try:
            stdout, stderr = process.communicate(input_text, timeout=timeout)
        except subprocess.TimeoutExpired as error:
            _terminate_process(process)
            raise RuntimeError(f"{Path(arguments[0]).name} exceeded the {_timeout_text(timeout)} safety limit") from error
    finally:
        _untrack_process(process)
    if process.returncode:
        detail = _command_failure_detail(stdout, stderr)
        raise RuntimeError(f"{Path(arguments[0]).name} failed with exit code {process.returncode}:\n{detail}")
    return (stdout or "").strip()

def _archive_structure_error(archive: zipfile.ZipFile) -> str:
    """Reject archive names that are unsafe or ambiguous on common filesystems."""
    names = set()
    manifest_count = 0
    expanded_size = 0
    for info in archive.infolist():
        raw = info.filename
        normalized = raw.replace("\\", "/")
        parts = normalized.split("/")
        if not raw or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized) or any(part == ".." for part in parts):
            return f"release package contains unsafe path {raw or '<empty>'}"
        key = normalized.rstrip("/").casefold()
        if key in names:
            return f"release package contains duplicate or case-colliding path {raw}"
        names.add(key)
        if normalized == "manifest.ini":
            manifest_count += 1
        if stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK:
            return f"release package contains unsupported symbolic link {raw}"
        if info.flag_bits & 0x1:
            return f"release package contains encrypted file {raw}"
        expanded_size += info.file_size
        if expanded_size > MAX_EXPANDED_PACKAGE_BYTES:
            return "release package exceeds the 512 MB expanded-size verification limit"
    if manifest_count != 1:
        return "release package must contain exactly one root manifest.ini"
    return ""


def _archive_integrity_error(archive: zipfile.ZipFile) -> str:
    """Read every member with bounded expansion so CRC failures cannot hide in binaries."""
    try:
        for info in archive.infolist():
            if info.is_dir():
                continue
            with archive.open(info) as member:
                while member.read(1024 * 1024):
                    pass
    except (OSError, RuntimeError, NotImplementedError, zipfile.BadZipFile) as error:
        return f"release package contains a damaged or unsupported file: {error}"
    return ""


def _package_python_error(payload: bytes, minimum_nvda: str) -> str:
    """Return why packaged Python cannot load in the oldest supported NVDA, or an empty string."""
    # Without a minimum version, still reject outright syntax errors.
    try: target = python_compatibility.python_version_for_nvda(minimum_nvda or "2026.1")
    except ValueError: return f"release package minimumNVDAVersion {minimum_nvda} is not a valid NVDA version"
    errors = []
    with zipfile.ZipFile(io.BytesIO(payload)) as archive:
        for info in archive.infolist():
            if info.is_dir() or Path(info.filename).suffix.casefold() != ".py": continue
            if info.file_size > 5 * 1024 * 1024:
                errors.append(f"{info.filename}: Python file exceeds the 5 MB validation limit"); continue
            try: source = archive.read(info).decode("utf-8-sig")
            except UnicodeError as error:
                errors.append(f"{info.filename}: {error}"); continue
            errors.extend(python_compatibility.compatibility_errors(source, info.filename, target))
            if len(errors) >= 5: break
    if not errors: return ""
    supported = f"NVDA {minimum_nvda}, the oldest supported version" if minimum_nvda else "NVDA"
    return f"release package Python cannot load in {supported}: " + "; ".join(errors[:5])

def _verify_release_payload(payload: bytes, expected_name: str, expected_version: str, api_versions=None, submission_channel="") -> tuple[bool, str, tuple[str, ...]]:
    """Verify the archive that will be, or already was, published."""
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            structure_error = _archive_structure_error(archive)
            if structure_error:
                return False, structure_error, ()
            integrity_error = _archive_integrity_error(archive)
            if integrity_error:
                return False, integrity_error, ()
            info = archive.getinfo("manifest.ini")
            if info.file_size > 1024 * 1024: return False, "release package manifest is unexpectedly large", ()
            manifest = archive.read(info).decode("utf-8-sig")
            ai_issues = _ai_disclosure_issues(archive)
        fields = {match.group(1).lower(): match.group(2).strip().strip('"\'') for match in re.finditer(r"^\s*([A-Za-z][A-Za-z0-9]*)\s*=\s*(.*?)\s*$", manifest, re.M)}
        if fields.get("name", "").casefold() != expected_name.casefold(): return False, f"release package contains add-on {fields.get('name') or 'unknown'}, expected {expected_name}", ()
        if normalized_version(fields.get("version", "")).casefold() != normalized_version(expected_version).casefold(): return False, f"release package version {fields.get('version') or 'unknown'} does not match local version {expected_version}", ()
        python_error = _package_python_error(payload, fields.get("minimumnvdaversion", ""))
        if python_error: return False, python_error, ()
        guideline_issues = _manifest_guideline_issues(fields, api_versions, submission_channel) if api_versions is not None else []
        return True, "", tuple(guideline_issues + ai_issues)
    except (OSError, ValueError, UnicodeError, zipfile.BadZipFile, KeyError) as error:
        return False, f"release package could not be verified: {error}", ()

def verify_release_package_file(path: Path, expected_name: str, expected_version: str) -> tuple[bool, str, tuple[str, ...]]:
    """Verify a local package immediately before it can be uploaded."""
    try:
        if path.stat().st_size > MAX_PACKAGE_BYTES: return False, "release package exceeds the 100 MB verification limit", ()
        return _verify_release_payload(path.read_bytes(), expected_name, expected_version)
    except OSError as error:
        return False, f"release package could not be verified: {error}", ()

def verify_release_package(url: str, expected_name: str, expected_version: str, api_versions=None, submission_channel="") -> tuple[bool, str, tuple[str, ...]]:
    """Download a release asset and verify its root manifest, not its cosmetic filename."""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "NVDA-Addon-Developer-Updater"})
        with urllib.request.urlopen(request, timeout=30) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > MAX_PACKAGE_BYTES: return False, "release package exceeds the 100 MB verification limit", ()
            payload = response.read(MAX_PACKAGE_BYTES + 1)
        if len(payload) > MAX_PACKAGE_BYTES: return False, "release package exceeds the 100 MB verification limit", ()
        return _verify_release_payload(payload, expected_name, expected_version, api_versions or {}, submission_channel)
    except (OSError, ValueError, UnicodeError, zipfile.BadZipFile, KeyError) as error:
        return False, f"release package could not be verified: {error}", ()

def gh_path() -> str:
    found = shutil.which("gh")
    if found: return found
    candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe"
    if candidate.is_file(): return str(candidate)
    raise GitHubCliRequired("GitHub CLI is not installed")

def _github_owned_repository_nodes(query: str, progress_message: str, progress=None) -> tuple[str, list[dict]]:
    gh = gh_path()
    if progress: progress("Checking GitHub sign-in status")
    try: _run([gh, "auth", "status", "--active", "--hostname", "github.com"])
    except RuntimeError as error:
        if "not logged" in str(error).casefold(): raise AuthenticationRequired("GitHub CLI is not signed in for NVDA") from error
        raise
    repositories = []
    owner = ""
    cursor = ""
    seen_cursors = set()
    page = 0
    while True:
        page += 1
        if progress: progress(f"{progress_message}, page {page}")
        arguments = [gh, "api", "graphql", "-f", f"query={query}"]
        if cursor: arguments.extend(["-F", f"endCursor={cursor}"])
        payload = json.loads(_run(arguments))
        errors = payload.get("errors") or []
        if errors:
            details = "; ".join(str(error.get("message") or error) for error in errors if isinstance(error, dict))
            raise RuntimeError(f"GitHub repository query failed: {details or 'unknown GraphQL error'}")
        viewer = payload.get("data", {}).get("viewer", {})
        if not isinstance(viewer, dict): raise RuntimeError("GitHub did not return the signed-in account")
        owner = str(viewer.get("login") or owner)
        connection = viewer.get("repositories") or {}
        repositories.extend(repository for repository in connection.get("nodes") or [] if isinstance(repository, dict))
        page_info = connection.get("pageInfo") or {}
        if not page_info.get("hasNextPage"): break
        cursor = str(page_info.get("endCursor") or "")
        if not cursor or cursor in seen_cursors: raise RuntimeError("GitHub repository pagination did not provide a new continuation cursor")
        seen_cursors.add(cursor)
    return owner, repositories

def github_addon_repositories(progress=None) -> tuple[str, list[GitHubAddonRepository]]:
    """List newest downloadable NVDA add-on assets owned by the active GitHub user."""
    owner, nodes = _github_owned_repository_nodes(_ADDON_REPOSITORIES_QUERY, "Loading released NVDA add-on files from GitHub", progress)
    repositories = []
    for repository in nodes:
        url = str(repository.get("url") or "").strip(); full_name = str(repository.get("nameWithOwner") or "").strip()
        if not url.startswith("https://github.com/") or not full_name: continue
        for release in (repository.get("releases") or {}).get("nodes") or []:
            if not isinstance(release, dict) or release.get("isDraft"): continue
            assets = [asset for asset in (release.get("releaseAssets") or {}).get("nodes") or [] if isinstance(asset, dict) and str(asset.get("name") or "").casefold().endswith(".nvda-addon") and str(asset.get("downloadUrl") or "").startswith("https://github.com/")]
            if not assets: continue
            for asset in assets:
                repositories.append(GitHubAddonRepository(
                    str(repository.get("name") or full_name.rsplit("/", 1)[-1]), full_name, url,
                    str(repository.get("description") or "").strip(), bool(repository.get("isPrivate")), bool(repository.get("isArchived")),
                    str(release.get("tagName") or ""), bool(release.get("isPrerelease")), str(asset.get("name") or ""), str(asset.get("downloadUrl") or ""), int(asset.get("size") or 0),
                ))
            break
    unique = {repository.download_url.casefold(): repository for repository in repositories}
    return owner, sorted(unique.values(), key=lambda repository: (repository.full_name.casefold(), repository.asset_name.casefold()))

def github_addon_projects(progress=None) -> tuple[str, list[GitHubAddonRepository]]:
    """List repositories owned by the active account that use a recognized NVDA add-on layout."""
    owner, nodes = _github_owned_repository_nodes(_ADDON_PROJECTS_QUERY, "Loading NVDA add-on projects from GitHub", progress)
    repositories = []
    for repository in nodes:
        if not any(repository.get(marker) for marker in _ADDON_REPOSITORY_MARKERS): continue
        url = str(repository.get("url") or "").strip(); full_name = str(repository.get("nameWithOwner") or "").strip()
        if not url.startswith("https://github.com/") or not full_name: continue
        repositories.append(GitHubAddonRepository(
            str(repository.get("name") or full_name.rsplit("/", 1)[-1]), full_name, url,
            str(repository.get("description") or "").strip(), bool(repository.get("isPrivate")), bool(repository.get("isArchived")),
            default_branch=str((repository.get("defaultBranchRef") or {}).get("name") or ""), issues_enabled=bool(repository.get("hasIssuesEnabled")),
        ))
    unique = {repository.full_name.casefold(): repository for repository in repositories}
    return owner, sorted(unique.values(), key=lambda repository: repository.full_name.casefold())

def _validated_repository(repository: GitHubAddonRepository) -> tuple[str, str]:
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository.full_name): raise RuntimeError("GitHub repository name is invalid")
    if not repository.default_branch: raise RuntimeError(f"{repository.full_name} has no default branch")
    return repository.full_name, repository.default_branch

def github_issue_templates(repository: GitHubAddonRepository) -> list[GitHubIssueTemplate]:
    """List editable issue-template files from a repository's default branch."""
    full_name, branch = _validated_repository(repository); gh = gh_path()
    endpoint = f"repos/{full_name}/git/trees/{urllib.parse.quote(branch, safe='')}"
    payload = json.loads(_run([gh, "api", "-X", "GET", endpoint, "-f", "recursive=1"]))
    if payload.get("truncated"): raise RuntimeError("GitHub returned an incomplete repository tree")
    prefix = ".github/ISSUE_TEMPLATE/"; templates = []
    for item in payload.get("tree") or []:
        path = str(item.get("path") or "") if isinstance(item, dict) and item.get("type") == "blob" else ""
        relative = path[len(prefix):] if path.startswith(prefix) else ""
        if not relative or "/" in relative or Path(relative).suffix.casefold() not in {".md", ".yml", ".yaml"}: continue
        templates.append(GitHubIssueTemplate(path, relative))
    return sorted(templates, key=lambda template: template.name.casefold())

def load_github_issue_template(repository: GitHubAddonRepository, template: GitHubIssueTemplate) -> GitHubIssueTemplate:
    full_name, branch = _validated_repository(repository)
    if template.path not in {item.path for item in github_issue_templates(repository)}: raise RuntimeError("The selected issue template is no longer available")
    endpoint = f"repos/{full_name}/contents/{urllib.parse.quote(template.path, safe='/')}"
    payload = json.loads(_run([gh_path(), "api", "-X", "GET", endpoint, "-f", f"ref={branch}"]))
    if payload.get("encoding") != "base64" or not payload.get("sha"): raise RuntimeError("GitHub returned an unsupported issue-template response")
    raw = base64.b64decode(re.sub(r"\s+", "", str(payload.get("content") or "")), validate=True)
    if len(raw) > 1024 * 1024: raise RuntimeError("The issue template exceeds the 1 MB editing limit")
    return GitHubIssueTemplate(template.path, template.name, str(payload["sha"]), raw.decode("utf-8-sig"))

def update_github_issue_template(repository: GitHubAddonRepository, template: GitHubIssueTemplate, content: str) -> None:
    full_name, branch = _validated_repository(repository)
    prefix = ".github/ISSUE_TEMPLATE/"; relative = template.path[len(prefix):] if template.path.startswith(prefix) else ""
    if not template.sha or not relative or "/" in relative or "\\" in relative or Path(relative).suffix.casefold() not in {".md", ".yml", ".yaml"}: raise RuntimeError("Issue-template revision information is missing or invalid")
    encoded = content.encode("utf-8")
    if not encoded or len(encoded) > 1024 * 1024: raise RuntimeError("The issue template must contain between 1 byte and 1 MB of UTF-8 text")
    request = json.dumps({"message": f"Update {template.name} issue template", "content": base64.b64encode(encoded).decode("ascii"), "sha": template.sha, "branch": branch})
    endpoint = f"repos/{full_name}/contents/{urllib.parse.quote(template.path, safe='/')}"
    _run([gh_path(), "api", "-X", "PUT", endpoint, "--input", "-"], input_text=request)

def delete_github_issue_template(repository: GitHubAddonRepository, template: GitHubIssueTemplate) -> None:
    """Delete an issue template only at the exact revision that was loaded."""
    full_name, branch = _validated_repository(repository)
    prefix = ".github/ISSUE_TEMPLATE/"; relative = template.path[len(prefix):] if template.path.startswith(prefix) else ""
    if not template.sha or not relative or "/" in relative or "\\" in relative or Path(relative).suffix.casefold() not in {".md", ".yml", ".yaml"}: raise RuntimeError("Issue-template revision information is missing or invalid")
    request = json.dumps({"message": f"Delete {template.name} issue template", "sha": template.sha, "branch": branch})
    endpoint = f"repos/{full_name}/contents/{urllib.parse.quote(template.path, safe='/')}"
    _run([gh_path(), "api", "-X", "DELETE", endpoint, "--input", "-"], input_text=request)

def parse_issue_template_document(template: GitHubIssueTemplate, content: str | None = None) -> IssueTemplateDocument:
    """Parse the editable parts of a GitHub issue form without discarding unknown keys."""
    text = template.content if content is None else content
    suffix = Path(template.name).suffix.casefold()
    if suffix in {".yml", ".yaml"}:
        data = yaml.load(text, Loader=_GitHubYamlLoader)
        if not isinstance(data, dict): raise ValueError("The YAML issue template must contain a mapping at its top level")
        if template.name.casefold() not in {"config.yml", "config.yaml"}:
            body = data.get("body", [])
            if not isinstance(body, list) or any(not isinstance(item, dict) for item in body): raise ValueError("The YAML issue form body must be a list of form elements")
        return IssueTemplateDocument("yaml", data)
    if suffix != ".md": raise ValueError("Only Markdown and YAML issue templates can be edited")
    data = {}; body = text
    if text.startswith("---"):
        match = re.match(r"^---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
        if not match: raise ValueError("The Markdown issue template has an unterminated YAML header")
        parsed = yaml.load(match.group(1), Loader=_GitHubYamlLoader) or {}
        if not isinstance(parsed, dict): raise ValueError("The Markdown issue-template header must contain a mapping")
        data = parsed; body = text[match.end():]
    return IssueTemplateDocument("markdown", data, body)

def issue_template_form_supported(template: GitHubIssueTemplate, document: IssueTemplateDocument | None, content: str | None = None) -> bool:
    """Return whether the accessible form can round-trip this template safely."""
    text = template.content if content is None else content
    if document is None or len(text.encode("utf-8")) > 256 * 1024 or template.name.casefold() in {"config.yml", "config.yaml"}:
        return False
    if document.kind == "markdown":
        return True
    if document.kind != "yaml":
        return False
    body = document.data.get("body", []) if isinstance(document.data, dict) else []
    return isinstance(body, list) and all(isinstance(item, dict) and item.get("type") in SUPPORTED_ISSUE_FORM_TYPES for item in body)

def render_issue_template_document(document: IssueTemplateDocument) -> str:
    """Render a form document as valid UTF-8 GitHub template text."""
    if document.kind == "yaml":
        return yaml.safe_dump(document.data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    if document.kind != "markdown": raise ValueError("Unknown issue-template format")
    header = yaml.safe_dump(document.data, allow_unicode=True, sort_keys=False, default_flow_style=False).rstrip()
    return f"---\n{header}\n---\n{document.markdown_body}"

def validate_issue_template_document(template: GitHubIssueTemplate, document: IssueTemplateDocument) -> None:
    """Reject form edits that GitHub's issue-form schema cannot accept."""
    if document.kind == "markdown": return
    if template.name.casefold() in {"config.yml", "config.yaml"}: return
    data = document.data
    for key in ("name", "description"):
        if not isinstance(data.get(key), str) or not data[key].strip(): raise ValueError(f"The issue form requires a non-empty {key} field")
    body = data.get("body")
    if not isinstance(body, list) or not body: raise ValueError("The issue form requires at least one form question")
    identifiers = set()
    for index, item in enumerate(body, 1):
        kind = item.get("type"); attributes = item.get("attributes")
        if kind not in SUPPORTED_ISSUE_FORM_TYPES: raise ValueError(f"Question {index} has unsupported type {kind!r}")
        if not isinstance(attributes, dict): raise ValueError(f"Question {index} requires an attributes section")
        if kind == "markdown":
            if not isinstance(attributes.get("value"), str) or not attributes["value"].strip(): raise ValueError(f"Markdown item {index} requires text")
            continue
        identifier = item.get("id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", identifier): raise ValueError(f"Question {index} requires a unique ID containing only letters, numbers, hyphens, or underscores")
        if identifier.casefold() in identifiers: raise ValueError(f"Question ID {identifier} is used more than once")
        identifiers.add(identifier.casefold())
        if not isinstance(attributes.get("label"), str) or not attributes["label"].strip(): raise ValueError(f"Question {index} requires a label")
        if kind in {"dropdown", "checkboxes"}:
            options = attributes.get("options")
            if not isinstance(options, list) or not options: raise ValueError(f"Question {index} requires at least one option")
            for option in options:
                label = option.get("label") if isinstance(option, dict) else option
                if not isinstance(label, str) or not label.strip(): raise ValueError(f"Question {index} contains an empty option")

def project_root(manifest: Path) -> Path:
    for candidate in (manifest.parent, *manifest.parents):
        if (candidate / ".git").exists(): return candidate
    return manifest.parent

def _git(root: Path, *arguments: str, timeout: int = COMMAND_TIMEOUT_SECONDS) -> str:
    return _run(["git", "-C", str(root), *arguments], timeout=timeout)

def _repository_remotes(root: Path) -> dict[str, str]:
    remotes = {}
    for name in filter(None, _git(root, "remote").splitlines()):
        try:
            remotes[name.strip()] = _git(root, "remote", "get-url", name.strip())
        except RuntimeError:
            continue
    return remotes


def _require_reviewed_push_url(root: Path, remote_name: str, reviewed_remote: str, display_name: str) -> str:
    """Reject a Git pushurl that redirects a reviewed remote to another repository."""
    push_url = _git(root, "remote", "get-url", "--push", remote_name)
    if _remote_web_url(push_url).casefold() != _remote_web_url(reviewed_remote).casefold():
        raise RuntimeError(f"{display_name} remote {remote_name} pushes to a different repository than the one reviewed; remove or correct its Git pushurl before publishing")
    return push_url

def _select_publish_remote(remotes: dict[str, str], authenticated_owner: str) -> tuple[str, str]:
    """Prefer a repository owned by the signed-in user, even when it is named fork."""
    for name, url in remotes.items():
        try:
            if repository_owner(url).casefold() == authenticated_owner.casefold():
                return name, url
        except RuntimeError:
            continue
    supported = {}
    for name, url in remotes.items():
        try:
            _remote_web_url(url)
        except RuntimeError:
            continue
        supported[name] = url
    if "origin" in supported:
        return "origin", supported["origin"]
    return next(iter(supported.items()), ("", ""))


def _matching_remote_name(remotes: dict[str, str], target: str) -> str:
    try:
        target_url = _remote_web_url(target).casefold()
    except RuntimeError:
        return ""
    for name, url in remotes.items():
        try:
            if _remote_web_url(url).casefold() == target_url:
                return name
        except RuntimeError:
            continue
    return ""


def _available_remote_name(remotes: dict[str, str], preferred: str = "origin") -> str:
    """Choose a deterministic remote name without colliding with local-only remotes."""
    if preferred not in remotes:
        return preferred
    number = 1
    while True:
        candidate = "publisher" if number == 1 else f"publisher{number}"
        if candidate not in remotes:
            return candidate
        number += 1


def _sensitive_files(root: Path) -> list[str]:
    names = _git(root, "ls-files", "-co", "--exclude-standard", "-z").split("\0")
    return [relative for relative in filter(None, names) if _is_sensitive_path(relative)]

def _is_sensitive_path(relative: str) -> bool:
    path = Path(relative); lower = path.name.casefold()
    return lower in SENSITIVE_NAMES or path.suffix.casefold() in SENSITIVE_SUFFIXES or lower.startswith(".env.")

def _is_generated_path(relative: str) -> bool:
    path = Path(relative)
    return path.suffix.casefold() in GENERATED_SUFFIXES or any(part.casefold() in GENERATED_DIRECTORIES for part in path.parts)

def _publishable_paths(root: Path) -> list[str]:
    names = _git(root, "ls-files", "-m", "-d", "-o", "--exclude-standard", "-z").split("\0")
    names += _git(root, "diff", "--cached", "--name-only", "-z").split("\0")
    return sorted({name for name in filter(None, names) if not _is_generated_path(name)})


def _staged_paths(root: Path) -> list[str]:
    return [name for name in _git(root, "diff", "--cached", "--name-only", "-z").split("\0") if name]


def _has_staged_changes(root: Path, display_name: str) -> bool:
    completed = subprocess.run(["git", "-C", str(root), "diff", "--cached", "--quiet"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if completed.returncode not in {0, 1}:
        detail = _command_failure_detail(completed.stdout, completed.stderr)
        raise RuntimeError(f"Git could not inspect staged changes for {display_name} (exit code {completed.returncode}): {detail}")
    return completed.returncode == 1


def _source_fingerprint(root: Path, has_git: bool | None = None) -> str:
    """Hash publishable source so a builder or concurrent edit cannot bypass review."""
    if has_git is None:
        has_git = (root / ".git").exists()
    if has_git:
        names = [name for name in _git(root, "ls-files", "-co", "--exclude-standard", "-z").split("\0") if name]
        paths = [(name, root / name) for name in names if not _is_generated_path(name)]
    else:
        paths = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            relative = path.relative_to(root).as_posix()
            if ".git" in Path(relative).parts or _is_generated_path(relative):
                continue
            paths.append((relative, path))
    digest = hashlib.sha256()
    for relative, path in sorted(paths, key=lambda item: item[0].casefold()):
        # A tracked file deleted from the working tree disappears from Git's
        # list once publishing commits the deletion; omit it in both states.
        if not os.path.lexists(path):
            continue
        digest.update(relative.replace("\\", "/").encode("utf-8", "surrogatepass")); digest.update(b"\0")
        try:
            if path.is_symlink():
                digest.update(b"link\0" + os.readlink(path).encode("utf-8", "surrogatepass"))
            else:
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
        except OSError as error:
            digest.update(f"unreadable:{error.__class__.__name__}".encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _require_source_fingerprint(root: Path, expected: str, has_git: bool, display_name: str) -> None:
    if expected and _source_fingerprint(root, has_git) != expected:
        raise RuntimeError(f"{display_name} source files changed after the publishing review; run the GitHub command again so the final files are reviewed and tested")

def _release_asset_urls(gh: str, remote: str, tag: str) -> list[str]:
    if not tag:
        return []
    payload = json.loads(_run([gh, "release", "view", tag, "--repo", _remote_web_url(remote), "--json", "assets"]) or "{}")
    return [
        str(asset.get("url") or "").strip()
        for asset in payload.get("assets") or []
        if isinstance(asset, dict) and str(asset.get("name") or "").casefold().endswith(".nvda-addon") and asset.get("url")
    ]


def _unpushed_status(root: Path, remote_name: str, default_branch: str) -> tuple[int, str]:
    """Return a proven unpushed count, or an explicit verification blocker."""
    try:
        if not remote_name or not default_branch:
            raise RuntimeError("the selected GitHub remote or its default branch is unknown")
        comparison = f"refs/remotes/{remote_name}/{default_branch}"
        _git(root, "fetch", "--no-tags", remote_name, f"+refs/heads/{default_branch}:{comparison}", timeout=TRANSFER_TIMEOUT_SECONDS)
        return int(_git(root, "rev-list", "--count", f"{comparison}..HEAD") or "0"), ""
    except (RuntimeError, ValueError) as error:
        return 0, f"local commit publication could not be verified: {error}"

def _verify_first_release_asset(urls: list[str], metadata: dict, api_versions: dict, channel: str) -> tuple[str, bool, str, tuple[str, ...]]:
    if not urls:
        return "", True, "", ()
    first_error = ""
    all_issues = ()
    for url in urls:
        verified, error, issues = verify_release_package(url, metadata.get("name", ""), metadata.get("version", ""), api_versions, channel)
        if verified:
            return url, True, "", issues
        if not first_error:
            first_error = error
            all_issues = issues
    return urls[0], False, first_error or "release package could not be verified", all_issues

def preflight(projects, values_reader, progress=None, cancelled=None) -> list[ProjectPublishInfo]:
    gh = gh_path()
    if progress: progress("Checking GitHub sign-in status")
    try: _run([gh, "auth", "status", "--active", "--hostname", "github.com"])
    except RuntimeError as error:
        if "not logged" in str(error).lower(): raise AuthenticationRequired("GitHub CLI is not signed in for NVDA") from error
        raise
    authenticated_owner = _run([gh, "api", "user", "--jq", ".login"]).strip()
    try: api_versions = nvda_api_versions()
    except Exception as error: raise RuntimeError(f"current NVDA Add-on Store API versions could not be loaded: {error}") from error
    reports = []
    for index, project in enumerate(projects, 1):
        _stop_if_cancelled(cancelled)
        manifest = Path(project.path).resolve(); root = project_root(manifest)
        metadata = values_reader(manifest); display_name = str(metadata.get("summary") or project.name)
        if progress: progress(f"Inspecting {display_name}, project {index} of {len(projects)}")
        has_git = (root / ".git").exists()
        current_branch = _git(root, "branch", "--show-current").strip() if has_git else "main"
        if has_git and not current_branch:
            raise RuntimeError(f"{display_name} is using a detached Git HEAD; check out the branch to publish before continuing")
        if not has_git:
            names = [path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()]
            unsafe = [name for name in names if _is_sensitive_path(name)]
        else: unsafe = _sensitive_files(root)
        if unsafe: raise RuntimeError(f"{display_name} contains files that may hold credentials: {', '.join(unsafe[:5])}")
        remote = ""
        remote_name = ""
        if has_git:
            remote_name, remote = _select_publish_remote(_repository_remotes(root), authenticated_owner)
        if not remote: remote = _manifest_github_remote(metadata)
        if not remote:
            try: remote = _run([gh, "repo", "view", _repository_name(display_name), "--json", "url", "--jq", ".url"])
            except RuntimeError: pass
        release_download = ""; package_verified = True; package_error = ""
        matching_tag = ""; matching_download = ""; matching_verified = True; matching_error = ""; matching_targets_head = True
        default_branch = ""
        if remote:
            repository_details = json.loads(_run([gh, "repo", "view", _remote_web_url(remote), "--json", "visibility,defaultBranchRef"]) or "{}")
            visibility = str(repository_details.get("visibility", ""))
            default_branch = str((repository_details.get("defaultBranchRef") or {}).get("name") or "")
            if visibility.upper() != "PUBLIC": raise RuntimeError(f"{display_name} must use a public GitHub repository for NVDA Store submission")
            if progress: progress(f"Checking the newest GitHub Release for {display_name}")
            release_rows = json.loads(_run([gh, "release", "list", "--repo", _remote_web_url(remote), "--exclude-drafts", "--limit", "1000", "--json", "tagName"]) or "[]")
            release_tags = [str(row.get("tagName") or "") for row in release_rows if isinstance(row, dict) and row.get("tagName")]
            github_release = release_tags[0] if release_tags else ""
            matching_tag = matching_release_tag(release_tags, metadata.get("version", ""))
            latest_urls = _release_asset_urls(gh, remote, github_release)
            matching_urls = latest_urls if matching_tag == github_release else _release_asset_urls(gh, remote, matching_tag)
            if matching_tag and has_git:
                tag_commit = _remote_tag_commit(root, remote_name or remote, matching_tag)
                matching_targets_head = bool(tag_commit) and tag_commit.casefold() == _git(root, "rev-parse", "HEAD").casefold()
        else: github_release = ""
        changed = len(_publishable_paths(root)) if has_git else len([name for name in names if not _is_generated_path(name)])
        unpushed = 0
        unpushed_error = ""
        if has_git and remote:
            unpushed, unpushed_error = _unpushed_status(root, remote_name, default_branch)
        owned = not remote or repository_owner(remote).casefold() == authenticated_owner.casefold()
        api_details = api_versions.get(metadata.get("lasttestednvdaversion", ""), {})
        version_lower = metadata.get("version", "").casefold()
        channel = "dev" if api_details.get("experimental") or "alpha" in version_lower or "dev" in version_lower else "beta" if "beta" in version_lower or "rc" in version_lower else "stable"
        guideline_issues = tuple(_source_disclosure_issues(root, has_git))
        if remote and owned:
            guideline_issues += tuple(_github_disclosure_issues(gh, remote, github_release))
        if remote and owned:
            release_download, package_verified, package_error, package_issues = _verify_first_release_asset(latest_urls, metadata, api_versions, channel)
            guideline_issues += package_issues
            if matching_tag == github_release:
                matching_download, matching_verified, matching_error = release_download, package_verified, package_error
            else:
                matching_download, matching_verified, matching_error, _matching_issues = _verify_first_release_asset(matching_urls, metadata, api_versions, channel)
        license_name, license_url = detect_license(root, manifest.parent)
        notes = release_notes(root, manifest.parent, metadata.get("version", ""), metadata.get("changelog", ""))
        reports.append(ProjectPublishInfo(project.project_id, project.name, str(manifest), str(root), metadata.get("version", ""), metadata.get("summary", project.name), metadata.get("author", ""), remote, changed, has_git, unpushed, normalized_version(github_release), release_download, owned, package_verified, package_error, guideline_issues, channel, default_branch, current_branch, matching_tag, matching_download, matching_verified, matching_error, matching_targets_head, unpushed_error, _source_fingerprint(root, has_git), license_name, license_url, notes))
    return reports

def login() -> None:
    gh = gh_path()
    process = subprocess.Popen(
        [gh, "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web", "--clipboard"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _track_process(process); output = ""
    try:
        try:
            process.stdin.write("y\n"); process.stdin.flush(); time.sleep(0.5)
            process.stdin.write("\n"); process.stdin.flush(); time.sleep(0.5)
            try: os.startfile(GITHUB_DEVICE_URL)
            except OSError:
                if not webbrowser.open(GITHUB_DEVICE_URL): raise RuntimeError("The GitHub device sign-in page could not be opened")
            output, _unused = process.communicate(timeout=600)
        except subprocess.TimeoutExpired as error:
            _terminate_process(process, 5)
            raise RuntimeError("GitHub sign-in timed out after 10 minutes") from error
        except Exception:
            _terminate_process(process, 5)
            raise
    finally:
        _untrack_process(process)
    if process.returncode: raise RuntimeError((output or "GitHub login failed").strip()[-2000:])
    _run([gh, "auth", "status", "--active", "--hostname", "github.com"])

def _remote_web_url(remote: str) -> str:
    value = remote.strip().rstrip("/")
    if value.startswith("git@github.com:"): value = "https://github.com/" + value.split(":", 1)[1]
    if value.endswith(".git"): value = value[:-4]
    if not value.startswith("https://github.com/"): raise RuntimeError(f"Unsupported GitHub remote: {remote}")
    return value

def repository_owner(remote: str) -> str:
    path = urllib.parse.urlsplit(_remote_web_url(remote)).path.strip("/")
    return urllib.parse.unquote(path.split("/", 1)[0]) if "/" in path else ""

def repository_url(remote: str) -> str:
    return _remote_web_url(remote)

def repository_name(remote: str) -> str:
    path = urllib.parse.urlsplit(_remote_web_url(remote)).path.strip("/")
    parts = path.split("/", 1)
    if len(parts) != 2 or not parts[1]: raise RuntimeError(f"GitHub repository name is missing from: {remote}")
    return urllib.parse.unquote(parts[1])

def _ancestor_result(root: Path, ancestor: str, descendant: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), "merge-base", "--is-ancestor", ancestor, descendant],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )

def release_target_status(root: Path, remote_name: str, default_branch: str, target_commit: str) -> tuple[str, bool]:
    """Return a release blocker and whether the default branch can be fast-forwarded."""
    if not remote_name or not default_branch:
        return "the repository's default branch could not be determined", False
    remote_reference = f"refs/remotes/{remote_name}/{default_branch}"
    try:
        _git(root, "fetch", "--no-tags", remote_name, f"+refs/heads/{default_branch}:{remote_reference}", timeout=TRANSFER_TIMEOUT_SECONDS)
        contained = _ancestor_result(root, target_commit, remote_reference)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        return f"the default-branch safety check failed: {error}", False
    if contained.returncode == 0:
        return "", False
    if contained.returncode != 1:
        detail = (contained.stderr or contained.stdout).strip()
        return f"Git could not verify the repository's default branch: {detail or f'exit code {contained.returncode}'}", False
    try:
        fast_forward = _ancestor_result(root, remote_reference, target_commit)
    except (OSError, subprocess.SubprocessError) as error:
        return f"the default-branch fast-forward check failed: {error}", False
    if fast_forward.returncode == 0:
        return f"the pushed commit is not yet on the repository's default branch, {default_branch}; that branch can be safely fast-forwarded", True
    if fast_forward.returncode == 1:
        return f"the pushed commit is not yet on the repository's default branch, {default_branch}, and the branches have diverged; merge a pull request before creating a release", False
    detail = (fast_forward.stderr or fast_forward.stdout).strip()
    return f"Git could not verify whether {default_branch} can be fast-forwarded: {detail or f'exit code {fast_forward.returncode}'}", False

def release_target_blocker(root: Path, remote_name: str, default_branch: str, target_commit: str) -> str:
    """Return why a pushed commit cannot safely be released, or an empty string."""
    return release_target_status(root, remote_name, default_branch, target_commit)[0]

def push(reports: list[ProjectPublishInfo], progress=None, cancelled=None) -> list[dict]:
    gh = gh_path(); published = []
    for index, report in enumerate(reports, 1):
        _stop_if_cancelled(cancelled, published)
        display_name = public_name(report)
        if release_publish_action(report) == "version-conflict":
            raise RuntimeError(f"{display_name} version {report.version} already has a GitHub Release that does not include the current local changes. Increment the manifest version before publishing the current code")
        if progress: progress(f"Preparing {display_name}, project {index} of {len(reports)}")
        root = Path(report.root)
        _require_source_fingerprint(root, report.source_fingerprint, report.has_git, display_name)
        if report.has_git:
            unsafe = _sensitive_files(root)
            branch = _git(root, "branch", "--show-current").strip()
            staged_excluded = [name for name in _staged_paths(root) if _is_generated_path(name)]
        else:
            names = [path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()]
            unsafe = [name for name in names if _is_sensitive_path(name)]
            branch = "main"; staged_excluded = []
        if unsafe:
            raise RuntimeError(f"{display_name} gained files that may hold credentials after the publishing review: {', '.join(unsafe[:5])}")
        if not branch:
            raise RuntimeError(f"{display_name} is using a detached Git HEAD; check out the branch to publish before continuing")
        if report.has_git and report.current_branch and branch != report.current_branch:
            raise RuntimeError(f"{display_name} changed branches after the publishing review, from {report.current_branch} to {branch}; run the command again")
        if staged_excluded:
            raise RuntimeError(f"{display_name} has generated files already staged in Git: {', '.join(staged_excluded[:5])}; unstage them before publishing")
        if not report.has_git:
            if progress: progress(f"Creating local Git repository for {display_name}")
            _run(["git", "init", "-b", "main"], root)
        push_remote = ""
        remote = report.remote
        remotes = _repository_remotes(root)
        if remote:
            push_remote = _matching_remote_name(remotes, remote)
            if not push_remote:
                push_remote = _available_remote_name(remotes, "publisher")
                _git(root, "remote", "add", push_remote, remote)
                remotes[push_remote] = remote
            # Perform this before staging or committing so a redirecting pushurl
            # cannot mutate the local repository before publication is stopped.
            _require_reviewed_push_url(root, push_remote, remote, display_name)
        publishable_paths = _publishable_paths(root)
        if publishable_paths:
            _git(root, "add", "--all", "--", *publishable_paths)
        staged = _has_staged_changes(root, display_name)
        if staged:
            if progress: progress(f"Committing changes for {display_name}")
            _git(root, "commit", "-m", "Prepare NVDA add-on store release")
        if not remote:
            if progress: progress(f"Finding or creating a public GitHub repository for {display_name}")
            repository = _repository_name(display_name)
            push_remote = _available_remote_name(remotes)
            try:
                existing = _run([gh, "repo", "view", repository, "--json", "url", "--jq", ".url"])
            except RuntimeError:
                _run([gh, "repo", "create", repository, "--public", "--source", str(root), "--remote", push_remote, "--description", report.summary])
            else:
                _git(root, "remote", "add", push_remote, existing + ".git")
            remote = _git(root, "remote", "get-url", push_remote)
        if progress: progress(f"Pushing {display_name} to GitHub")
        _require_reviewed_push_url(root, push_remote, remote, display_name)
        _git(root, "push", "--set-upstream", push_remote, branch, timeout=TRANSFER_TIMEOUT_SECONDS)
        # Releases must target the commit we actually pushed.  GitHub otherwise
        # defaults to the repository's default branch, which may be unrelated.
        target_commit = _git(root, "rev-parse", "HEAD")
        repository = _remote_web_url(remote)
        default_branch = report.default_branch
        release_pending = bool(release_publish_action(report))
        if release_pending:
            try:
                details = json.loads(_run([gh, "repo", "view", repository, "--json", "defaultBranchRef"]) or "{}")
                current_default_branch = str((details.get("defaultBranchRef") or {}).get("name") or "")
                if current_default_branch:
                    default_branch = current_default_branch
            except (RuntimeError, TypeError, ValueError):
                pass
        blocker, can_fast_forward = release_target_status(root, push_remote, default_branch, target_commit) if release_pending else ("", False)
        published.append({**report.__dict__, "sourceUrl": repository, "targetCommitish": target_commit, "currentBranch": branch, "defaultBranch": default_branch, "pushRemote": push_remote, "releaseBlocker": blocker, "defaultBranchCanFastForward": can_fast_forward})
        if progress: progress(f"GitHub push finished for {display_name}, project {index} of {len(reports)}")
    return published

def promote_default_branches(items: list[dict], progress=None, cancelled=None) -> list[dict]:
    """Fast-forward confirmed repository default branches without checking out or merging locally."""
    promoted = []
    for index, item in enumerate(items, 1):
        _stop_if_cancelled(cancelled, promoted)
        updated = dict(item); name = public_name(item); root = Path(item["root"])
        remote_name = str(item.get("pushRemote", "")); default_branch = str(item.get("defaultBranch", "")); target_commit = str(item.get("targetCommitish", ""))
        if progress: progress(f"Rechecking the default branch for {name}, project {index} of {len(items)}")
        blocker, can_fast_forward = release_target_status(root, remote_name, default_branch, target_commit)
        if not blocker:
            updated.update(releaseBlocker="", defaultBranchCanFastForward=False); promoted.append(updated); continue
        if not can_fast_forward:
            updated.update(releaseBlocker=blocker, defaultBranchCanFastForward=False); promoted.append(updated); continue
        try:
            if progress: progress(f"Fast-forwarding {default_branch} for {name}, project {index} of {len(items)}")
            _require_reviewed_push_url(root, remote_name, str(item.get("sourceUrl", "")), name)
            _git(root, "push", remote_name, f"{target_commit}:refs/heads/{default_branch}", timeout=TRANSFER_TIMEOUT_SECONDS)
            blocker, can_fast_forward = release_target_status(root, remote_name, default_branch, target_commit)
            if blocker:
                blocker = f"the default branch was pushed but could not be verified afterward: {blocker}"
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            blocker = f"GitHub did not accept the update to {default_branch}: {error}; create and merge a pull request instead"
            can_fast_forward = False
        updated.update(releaseBlocker=blocker, defaultBranchCanFastForward=can_fast_forward)
        promoted.append(updated)
    return promoted

def pull_request_url(item: dict) -> str:
    repository = str(item.get("sourceUrl", "")).rstrip("/")
    default_branch = str(item.get("defaultBranch", "")).strip(); current_branch = str(item.get("currentBranch", "")).strip()
    if not repository or not default_branch or not current_branch or default_branch == current_branch:
        return ""
    return f"{repository}/compare/{urllib.parse.quote(default_branch, safe='')}...{urllib.parse.quote(current_branch, safe='')}?expand=1"

def _package_source(manifest: Path) -> Path:
    return manifest.parent


def _safe_package_filename(name: str, version: str) -> str:
    """Return a portable package filename without permitting path traversal."""
    invalid = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
    for label, value in (("add-on name", str(name or "")), ("version", str(version or ""))):
        if not value or value in {".", ".."} or invalid.search(value) or value.rstrip(" .") != value:
            raise RuntimeError(f"The manifest {label} cannot be used in a safe release-package filename: {value or 'missing'}")
    return f"{name}-{version}.nvda-addon"


def build_package(item: dict, output_folder: Path) -> Path:
    manifest = Path(item["manifest"]); source = _package_source(manifest); output_folder = output_folder.resolve(); output_folder.mkdir(parents=True, exist_ok=True)
    filename = _safe_package_filename(item["name"], item["version"]); output = (output_folder / filename).resolve()
    if output.parent != output_folder:
        raise RuntimeError("The release-package destination is outside the selected output folder")
    files = {}
    roots = [(source, False)]
    if (source / "addon").is_dir(): roots.append((source / "addon", True))
    for root, is_addon_source in roots:
        for child in root.iterdir():
            if child.name.lower() not in RUNTIME_NAMES: continue
            if child.is_file(): files[child.name] = child
            else:
                for path in child.rglob("*"):
                    if path.is_file() and "__pycache__" not in path.parts and path.suffix.lower() not in {".pyc", ".pyo"}:
                        files[path.relative_to(root).as_posix()] = path
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for relative, path in sorted(files.items()): archive.write(path, relative)
    with zipfile.ZipFile(output) as archive:
        if "manifest.ini" not in archive.namelist(): raise RuntimeError(f"Package for {item['name']} has no root manifest.ini")
    return output

def builder_accepts_version(script: Path) -> bool:
    """Return whether a PowerShell builder declares a script-level Version parameter."""
    try: text = script.read_text(encoding="utf-8-sig")
    except (OSError, UnicodeError): return False
    text = re.sub(r"(?s)<#.*?#>", "", text); text = re.sub(r"(?m)#.*$", "", text)
    match = re.search(r"(?i)\bparam\s*\(", text)
    # A script parameter block may follow only attributes such as [CmdletBinding()].
    if not match or not re.fullmatch(r"(?s)\s*(?:\[[^\]]*\]\s*)*", text[:match.start()]): return False
    depth = 0
    for index in range(match.end() - 1, len(text)):
        depth += {"(": 1, ")": -1}.get(text[index], 0)
        if depth == 0:
            return bool(re.search(r"(?i)\$Version\b", text[match.end():index]))
    return False

def build_release_package(item: dict, output_folder: Path) -> Path:
    """Use a project's audited builder when it supplies one; otherwise use the safe generic builder."""
    source = _package_source(Path(item["manifest"]))
    project = Path(item.get("root") or source)
    build_script = project / "build.ps1"
    if not build_script.is_file():
        return build_package(item, output_folder)
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        raise RuntimeError(f"PowerShell is required to run the project builder for {item['name']}")
    outputs = project / "outputs"
    previous = {
        path.resolve(): (path.stat().st_mtime_ns, path.stat().st_size)
        for path in outputs.glob("*.nvda-addon")
    } if outputs.is_dir() else {}
    before_build = _source_fingerprint(project)
    arguments = [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(build_script)]
    # Builders without a Version parameter still run; the package filename and
    # its root manifest are checked against the expected version afterward.
    if builder_accepts_version(build_script): arguments += ["-Version", str(item["version"])]
    _run(arguments, cwd=project, timeout=BUILDER_TIMEOUT_SECONDS)
    if _source_fingerprint(project) != before_build:
        raise RuntimeError(f"The project builder for {item['name']} changed source files; review those changes and run publishing again")
    candidates = [
        path for path in outputs.glob("*.nvda-addon")
        if str(item["version"]).casefold() in path.name.casefold()
        and previous.get(path.resolve()) != (path.stat().st_mtime_ns, path.stat().st_size)
    ]
    if not candidates:
        raise RuntimeError(f"The project builder for {item['name']} produced no package for version {item['version']}")
    package = max(candidates, key=lambda path: path.stat().st_mtime_ns)
    output_folder.mkdir(parents=True, exist_ok=True)
    destination = output_folder / package.name
    if package.resolve() != destination.resolve():
        shutil.copy2(package, destination)
    return destination

def validate_publish_builds(reports: list[ProjectPublishInfo], progress=None, cancelled=None) -> None:
    """Prove releasable projects build successfully before changing GitHub."""
    pending = [report for report in reports if release_publish_action(report) in {"create", "repair"}]
    for index, report in enumerate(pending, 1):
        _stop_if_cancelled(cancelled)
        display_name = public_name(report)
        if progress:
            progress(f"Validating release build for {display_name}, project {index} of {len(pending)}")
        root = Path(report.root)
        package = build_release_package(report.__dict__, root / "outputs")
        verified, error, issues = verify_release_package_file(package, report.name, report.version)
        if not verified or issues:
            detail = error or "; ".join(issues)
            raise RuntimeError(f"The release package for {display_name} did not pass validation: {detail}")

def release_create_arguments(gh: str, tag: str, package: Path, repository: str, target: str, release_name: str, prerelease: bool = False, notes: str = "") -> list[str]:
    # Preflight compares the local version with the latest GitHub release, and
    # GitHub rejects duplicate tags.  Do not use --fail-on-no-commits here: gh
    # can falsely report no commits when --target is a SHA from a non-default
    # branch, and a version-only/package-only release is valid in any event.
    # Never use GitHub-generated notes: they can import unreviewed commit
    # messages into a public release after the submission audit has completed.
    # Reviewed local changelog notes are read from standard input; otherwise
    # fixed factual text is used.
    notes_arguments = ["--notes-file", "-"] if notes else ["--notes", f"Release package for {release_name} {tag.removeprefix('v')}."]
    arguments = [gh, "release", "create", tag, str(package), "--repo", repository, "--target", target, "--title", f"{release_name} {tag.removeprefix('v')}", *notes_arguments]
    if prerelease:
        arguments.append("--prerelease")
    return arguments

def _remote_tag_commit(root: Path, remote_name: str, tag: str) -> str:
    """Resolve a lightweight or annotated remote tag without changing local refs."""
    if not remote_name or not tag:
        return ""
    output = _git(root, "ls-remote", remote_name, f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}")
    direct = ""
    for line in output.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        commit, reference = parts
        if reference.endswith("^{}"):
            return commit
        direct = commit
    return direct

def _uploaded_asset_url(gh: str, repository: str, tag: str, package_name: str) -> str:
    payload = json.loads(_run([gh, "release", "view", tag, "--repo", repository, "--json", "assets"]) or "{}")
    for asset in payload.get("assets") or []:
        if isinstance(asset, dict) and str(asset.get("name") or "").casefold() == package_name.casefold():
            return str(asset.get("url") or "")
    return ""


def _require_release_source(item: dict, display_name: str) -> None:
    """Prove release bytes still come from the reviewed and pushed commit."""
    root = Path(item["root"])
    has_git = bool(item.get("has_git", (root / ".git").exists()))
    _require_source_fingerprint(root, str(item.get("source_fingerprint", "")), has_git, display_name)
    if has_git:
        target = str(item.get("targetCommitish", "")).strip()
        head = _git(root, "rev-parse", "HEAD").strip()
        if not target or head.casefold() != target.casefold():
            raise RuntimeError(f"{display_name} is no longer at the commit that was pushed and reviewed; run the GitHub command again")


def release(items: list[dict], output_folder: Path, progress=None, on_released=None, cancelled=None) -> list[dict]:
    gh = gh_path(); released = []
    for index, item in enumerate(items, 1):
        _stop_if_cancelled(cancelled, released)
        display_name = public_name(item)
        if item.get("releaseBlocker"):
            raise RuntimeError(f"Release creation was stopped for {display_name}: {item['releaseBlocker']}")
        action = release_publish_action(item)
        if action == "version-conflict":
            raise RuntimeError(f"{display_name} version {item['version']} already has a GitHub Release that does not include the current local changes. Increment the manifest version before publishing the current code")
        _require_release_source(item, display_name)
        if progress: progress(f"Packaging {display_name}, release {index} of {len(items)}")
        package = build_release_package(item, output_folder)
        verified, package_error, package_issues = verify_release_package_file(package, item["name"], item["version"])
        if not verified or package_issues:
            detail = package_error or "; ".join(package_issues)
            raise RuntimeError(f"The release package for {display_name} did not pass validation: {detail}")
        tag = str(item.get("matching_release_tag") or f"v{item['version']}")
        repository = item["sourceUrl"].removeprefix("https://github.com/")
        target = str(item.get("targetCommitish", "")).strip()
        if not target: raise RuntimeError(f"No pushed commit was recorded for {display_name}; release creation was stopped")
        _require_release_source(item, display_name)
        remote_tag = _remote_tag_commit(Path(item["root"]), str(item.get("pushRemote", "")), tag)
        if remote_tag and remote_tag.casefold() != target.casefold():
            raise RuntimeError(f"GitHub tag {tag} for {display_name} already points to a different commit. Increment the manifest version before publishing a new release")
        version = item["version"].lower()
        prerelease = item.get("channel") in {"beta", "dev"} or any(stage in version for stage in ("alpha", "beta", "dev", "rc"))
        if action == "repair":
            if progress: progress(f"Repairing GitHub Release {tag} for {display_name}")
            _run([gh, "release", "upload", tag, str(package), "--repo", repository, "--clobber"], timeout=TRANSFER_TIMEOUT_SECONDS)
        elif action == "create":
            notes = str(item.get("release_notes") or "")
            arguments = release_create_arguments(gh, tag, package, repository, target, item.get("summary") or item["name"], prerelease, notes)
            if progress: progress(f"Uploading GitHub Release {tag} for {display_name}")
            _run(arguments, input_text=notes or None, timeout=TRANSFER_TIMEOUT_SECONDS)
        else:
            raise RuntimeError(f"GitHub Release {tag} for {display_name} is already complete; no release change is needed")
        download = _uploaded_asset_url(gh, repository, tag, package.name)
        if not download:
            raise RuntimeError(f"GitHub accepted release {tag} for {display_name}, but the uploaded package could not be verified on the release")
        released_item = {**item, "package": str(package), "downloadUrl": download, "matching_release_tag": tag, "matching_release_download_url": download, "matching_release_package_verified": True, "matching_release_package_error": ""}
        released.append(released_item)
        # GitHub has already accepted this release.  Persist it before attempting
        # the next item so a later failure cannot make this success look missing.
        if on_released is not None: on_released(released_item)
        if progress: progress(f"GitHub Release published for {display_name}, release {index} of {len(items)}")
    return released

def store_url(item: dict, publisher_override: str = "", fallback_license_name: str = "", fallback_license_url: str = "") -> str:
    """Build a prefilled Store form; unknown publisher or license fields stay blank for manual entry."""
    version = item["version"].lower(); channel = item.get("channel") or ("dev" if "dev" in version or "alpha" in version else "beta" if "beta" in version or "rc" in version else "stable")
    submission_name = repository_name(item["sourceUrl"])
    license_name = str(item.get("license_name") or "") or fallback_license_name.strip()
    license_url = str(item.get("license_url") or "") if item.get("license_name") else fallback_license_url.strip()
    fields = {
        "title": f"[Submit add-on]: {submission_name} {item['version']}", "download-url": item["downloadUrl"],
        "source-url": item["sourceUrl"], "publisher": publisher_override.strip() or store_publisher_name(item.get("publisher", "")), "channel": channel,
        "license-name": license_name, "license-url": license_url,
    }
    return STORE_FORM + "&" + urllib.parse.urlencode(fields)

def valid_store_release(item: dict, local_version: str) -> bool:
    if not isinstance(item, dict) or not local_version or normalized_version(item.get("version", "")).casefold() != normalized_version(local_version).casefold(): return False
    try:
        source = urllib.parse.urlsplit(str(item.get("sourceUrl", ""))); download = urllib.parse.urlsplit(str(item.get("downloadUrl", "")))
    except ValueError: return False
    if source.scheme != "https" or source.netloc.casefold() != "github.com" or download.scheme != "https" or download.netloc.casefold() != "github.com": return False
    repository_path = source.path.rstrip("/")
    release_prefix = repository_path + "/releases/download/"
    if not repository_path or not download.path.startswith(release_prefix): return False
    release_suffix = download.path[len(release_prefix):]
    encoded_tag = release_suffix.split("/", 1)[0] if "/" in release_suffix else ""
    release_tag = urllib.parse.unquote(encoded_tag)
    normalized_tag = normalized_version(release_tag)
    if normalized_tag.casefold().startswith("release-"): normalized_tag = normalized_tag[8:]
    asset_name = Path(urllib.parse.unquote(download.path)).name.casefold()
    return bool(item.get("name") and normalized_tag.casefold() == normalized_version(local_version).casefold() and asset_name.endswith(".nvda-addon"))
