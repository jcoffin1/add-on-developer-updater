from __future__ import annotations

import base64, io, json, os, re, shutil, subprocess, tempfile, time, urllib.parse, urllib.request, webbrowser, zipfile
from dataclasses import dataclass
from pathlib import Path

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
STORE_PUBLISHER = "Justin Coffin"
GITHUB_DEVICE_URL = "https://github.com/login/device"
NVDA_API_VERSIONS_URL = "https://raw.githubusercontent.com/nvaccess/addon-datastore/master/transform/nvdaAPIVersions.json"
RUNTIME_NAMES = {"appmodules", "brailledisplaydrivers", "copying.txt", "doc", "globalplugins", "installtasks.py", "license.txt", "locale", "manifest.ini", "synthdrivers"}
SENSITIVE_NAMES = {".env", "credentials.json", "id_dsa", "id_ed25519", "id_rsa", "secrets.json"}
SENSITIVE_SUFFIXES = {".key", ".p12", ".pem", ".pfx"}
GENERATED_DIRECTORIES = {"__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", "build", "build-local", "dist", "outputs"}
GENERATED_SUFFIXES = {".pyc", ".pyo"}
_AI_DISCLOSURE_PATTERNS = (
    re.compile(r"\b" + "A" + "I" + r"\b", re.I),
    re.compile(r"\bAI(?:[- ]generated|[- ]written|[- ]authored)\b", re.I),
    re.compile(r"\b(?:generated|written|authored|created)\s+(?:by|with|using)\s+(?:an?\s+)?(?:" + "A" + "I" + r"|ChatGPT|OpenAI|Codex|Copilot)\b", re.I),
    re.compile(r"\b(?:" + "A" + "I" + r" agent|artificial " + "intelligence" + r"|large language " + "model" + r"|LLM-" + "generated" + r")\b", re.I),
    re.compile(r"^\s*co-authored-by:.*(?:ChatGPT|OpenAI|Codex|Copilot)", re.I),
)

class AuthenticationRequired(RuntimeError):
    pass

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
    item = {**report.__dict__, "sourceUrl": repository_url(report.remote) if report.remote else "", "downloadUrl": report.github_release_download_url}
    if report.remote and report.repository_owned_by_user and not valid_store_release(item, report.version):
        if not report.github_release_version: reasons.append("no GitHub Release")
        elif release_needed(report.version, report.github_release_version): reasons.append(f"GitHub Release {report.github_release_version} does not match local version {report.version}")
        elif not report.github_release_download_url: reasons.append("matching release has no .nvda-addon asset")
    release_matches_local = bool(report.github_release_version) and not release_needed(report.version, report.github_release_version)
    if release_matches_local and report.github_release_download_url and report.repository_owned_by_user and not report.release_package_verified:
        reasons.append(report.release_package_error or "release package manifest could not be verified")
    if report.changed_files: reasons.append(f"{report.changed_files} local changed {'file is' if report.changed_files == 1 else 'files are'} not included in the release")
    if report.unpushed_commits: reasons.append(f"{report.unpushed_commits} local commits are not pushed")
    reasons.extend(report.store_guideline_issues)
    return reasons

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
    return (value or "").strip().removeprefix("v").removeprefix("V")

def release_needed(local_version: str, github_version: str) -> bool:
    return normalized_version(local_version).lower() != normalized_version(github_version).lower()

def _repository_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-.") or "nvda-addon"

def _manifest_github_remote(metadata: dict) -> str:
    for key in ("url", "sourceurl", "sourcecodeurl", "repository"):
        value = str(metadata.get(key, "")).strip()
        if value.startswith("https://github.com/") or value.startswith("git@github.com:"): return value
    return ""

def _run(arguments: list[str], cwd: Path | None = None, input_text: str | None = None) -> str:
    completed = subprocess.run(arguments, cwd=cwd, input=input_text, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if completed.returncode:
        detail = (completed.stderr or completed.stdout).strip()
        raise RuntimeError(f"{Path(arguments[0]).name} failed: {detail}"[:2000])
    return completed.stdout.strip()

def verify_release_package(url: str, expected_name: str, expected_version: str, api_versions=None, submission_channel="") -> tuple[bool, str, tuple[str, ...]]:
    """Download a release asset and verify its root manifest, not its cosmetic filename."""
    maximum = 100 * 1024 * 1024
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "NVDA-Addon-Developer-Updater"})
        with urllib.request.urlopen(request, timeout=30) as response:
            length = response.headers.get("Content-Length")
            if length and int(length) > maximum: return False, "release package exceeds the 100 MB verification limit", ()
            payload = response.read(maximum + 1)
        if len(payload) > maximum: return False, "release package exceeds the 100 MB verification limit", ()
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            info = archive.getinfo("manifest.ini")
            if info.file_size > 1024 * 1024: return False, "release package manifest is unexpectedly large", ()
            manifest = archive.read(info).decode("utf-8-sig")
            ai_issues = _ai_disclosure_issues(archive)
        fields = {match.group(1).lower(): match.group(2).strip().strip('"\'') for match in re.finditer(r"^\s*([A-Za-z][A-Za-z0-9]*)\s*=\s*(.*?)\s*$", manifest, re.M)}
        if fields.get("name", "").casefold() != expected_name.casefold(): return False, f"release package contains add-on {fields.get('name') or 'unknown'}, expected {expected_name}", ()
        if normalized_version(fields.get("version", "")).casefold() != normalized_version(expected_version).casefold(): return False, f"release package version {fields.get('version') or 'unknown'} does not match local version {expected_version}", ()
        return True, "", tuple(_manifest_guideline_issues(fields, api_versions or {}, submission_channel) + ai_issues)
    except (OSError, ValueError, UnicodeError, zipfile.BadZipFile, KeyError) as error:
        return False, f"release package could not be verified: {error}", ()

def gh_path() -> str:
    found = shutil.which("gh")
    if found: return found
    candidate = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "GitHub CLI" / "gh.exe"
    if candidate.is_file(): return str(candidate)
    raise RuntimeError("GitHub CLI is not installed")

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
    identifiers = set(); supported = {"input", "textarea", "dropdown", "checkboxes", "markdown"}
    for index, item in enumerate(body, 1):
        kind = item.get("type"); attributes = item.get("attributes")
        if kind not in supported: raise ValueError(f"Question {index} has unsupported type {kind!r}")
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

def _git(root: Path, *arguments: str) -> str:
    return _run(["git", "-C", str(root), *arguments])

def _repository_remotes(root: Path) -> dict[str, str]:
    remotes = {}
    for name in filter(None, _git(root, "remote").splitlines()):
        try:
            remotes[name.strip()] = _git(root, "remote", "get-url", name.strip())
        except RuntimeError:
            continue
    return remotes

def _select_publish_remote(remotes: dict[str, str], authenticated_owner: str) -> tuple[str, str]:
    """Prefer a repository owned by the signed-in user, even when it is named fork."""
    for name, url in remotes.items():
        try:
            if repository_owner(url).casefold() == authenticated_owner.casefold():
                return name, url
        except RuntimeError:
            continue
    if "origin" in remotes:
        return "origin", remotes["origin"]
    return next(iter(remotes.items()), ("", ""))

def _sensitive_files(root: Path) -> list[str]:
    names = _git(root, "ls-files", "-co", "--exclude-standard", "-z").split("\0")
    unsafe = []
    for relative in filter(None, names):
        path = Path(relative); lower = path.name.lower()
        if lower in SENSITIVE_NAMES or path.suffix.lower() in SENSITIVE_SUFFIXES or lower.startswith(".env."):
            unsafe.append(relative)
    return unsafe

def _is_generated_path(relative: str) -> bool:
    path = Path(relative)
    return path.suffix.casefold() in GENERATED_SUFFIXES or any(part.casefold() in GENERATED_DIRECTORIES for part in path.parts)

def _publishable_paths(root: Path) -> list[str]:
    names = _git(root, "ls-files", "-m", "-d", "-o", "--exclude-standard", "-z").split("\0")
    return [name for name in filter(None, names) if not _is_generated_path(name)]

def preflight(projects, values_reader, progress=None) -> list[ProjectPublishInfo]:
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
        if progress: progress(f"Inspecting {project.name}, project {index} of {len(projects)}")
        manifest = Path(project.path).resolve(); root = project_root(manifest)
        has_git = (root / ".git").exists()
        if not has_git:
            names = [path.name for path in root.rglob("*") if path.is_file()]
            unsafe = [name for name in names if name.lower() in SENSITIVE_NAMES or Path(name).suffix.lower() in SENSITIVE_SUFFIXES]
        else: unsafe = _sensitive_files(root)
        if unsafe: raise RuntimeError(f"{project.name} contains files that may hold credentials: {', '.join(unsafe[:5])}")
        metadata = values_reader(manifest)
        remote = ""
        if has_git:
            _remote_name, remote = _select_publish_remote(_repository_remotes(root), authenticated_owner)
        if not remote: remote = _manifest_github_remote(metadata)
        if not remote:
            try: remote = _run([gh, "repo", "view", _repository_name(project.name), "--json", "url", "--jq", ".url"])
            except RuntimeError: pass
        release_download = ""; package_verified = True; package_error = ""
        default_branch = ""
        if remote:
            repository_details = json.loads(_run([gh, "repo", "view", _remote_web_url(remote), "--json", "visibility,defaultBranchRef"]) or "{}")
            visibility = str(repository_details.get("visibility", ""))
            default_branch = str((repository_details.get("defaultBranchRef") or {}).get("name") or "")
            if visibility.upper() != "PUBLIC": raise RuntimeError(f"{project.name} must use a public GitHub repository for NVDA Store submission")
            if progress: progress(f"Checking the newest GitHub Release for {project.name}")
            github_release = _run([gh, "release", "list", "--repo", _remote_web_url(remote), "--exclude-drafts", "--limit", "1", "--json", "tagName", "--jq", ".[0].tagName"])
            if github_release:
                assets = _run([gh, "release", "view", github_release, "--repo", _remote_web_url(remote), "--json", "assets", "--jq", '.assets[] | select(.name | endswith(".nvda-addon")) | .url'])
                release_download = next((line.strip() for line in assets.splitlines() if line.strip()), "")
        else: github_release = ""
        changed = len(_publishable_paths(root)) if has_git else len(names)
        unpushed = 0
        if has_git and remote:
            try: unpushed = int(_git(root, "rev-list", "--count", "@{upstream}..HEAD") or "0")
            except (RuntimeError, ValueError):
                try:
                    # A topic branch may have been deleted after its commit was merged.
                    # Do not label the repository's entire history as unpushed merely
                    # because that local branch no longer has an upstream.
                    containing = _git(root, "branch", "--remotes", "--contains", "HEAD")
                    if containing:
                        unpushed = 0
                    else:
                        unpushed = int(_git(root, "rev-list", "--count", "refs/remotes/origin/HEAD..HEAD") or "0")
                except (RuntimeError, ValueError): unpushed = 0
        owned = not remote or repository_owner(remote).casefold() == authenticated_owner.casefold()
        api_details = api_versions.get(metadata.get("lasttestednvdaversion", ""), {})
        version_lower = metadata.get("version", "").casefold()
        channel = "dev" if api_details.get("experimental") or "alpha" in version_lower or "dev" in version_lower else "beta" if "beta" in version_lower or "rc" in version_lower else "stable"
        guideline_issues = tuple(_source_disclosure_issues(root, has_git))
        if remote and owned:
            guideline_issues += tuple(_github_disclosure_issues(gh, remote, github_release))
        if release_download and owned:
            package_verified, package_error, package_issues = verify_release_package(release_download, metadata.get("name", project.name), metadata.get("version", ""), api_versions, channel)
            guideline_issues += package_issues
        reports.append(ProjectPublishInfo(project.project_id, project.name, str(manifest), str(root), metadata.get("version", ""), metadata.get("summary", project.name), metadata.get("author", ""), remote, changed, has_git, unpushed, normalized_version(github_release), release_download, owned, package_verified, package_error, guideline_issues, channel, default_branch))
    return reports

def login() -> None:
    gh = gh_path()
    process = subprocess.Popen(
        [gh, "auth", "login", "--hostname", "github.com", "--git-protocol", "https", "--web", "--clipboard"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    try:
        process.stdin.write("y\n"); process.stdin.flush(); time.sleep(0.5)
        process.stdin.write("\n"); process.stdin.flush(); time.sleep(0.5)
        try: os.startfile(GITHUB_DEVICE_URL)
        except OSError:
            if not webbrowser.open(GITHUB_DEVICE_URL): raise RuntimeError("The GitHub device sign-in page could not be opened")
        process.stdin.close(); process.wait(timeout=600); output = process.stdout.read()
    except subprocess.TimeoutExpired:
        process.terminate(); raise RuntimeError("GitHub sign-in timed out after 10 minutes")
    if process.returncode: raise RuntimeError((output or "GitHub login failed").strip()[-2000:])
    _run([gh, "auth", "status", "--active", "--hostname", "github.com"])

def _remote_web_url(remote: str) -> str:
    value = remote.strip()
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

def release_target_blocker(root: Path, remote_name: str, default_branch: str, target_commit: str) -> str:
    """Return why a pushed commit cannot safely be released, or an empty string."""
    if not remote_name or not default_branch:
        return "the repository's default branch could not be determined"
    remote_reference = f"refs/remotes/{remote_name}/{default_branch}"
    try:
        _git(root, "fetch", "--no-tags", remote_name, f"+refs/heads/{default_branch}:{remote_reference}")
        completed = subprocess.run(
            ["git", "-C", str(root), "merge-base", "--is-ancestor", target_commit, remote_reference],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        return f"the default-branch safety check failed: {error}"
    if completed.returncode == 0:
        return ""
    if completed.returncode == 1:
        return f"the pushed commit is not yet on the repository's default branch, {default_branch}; merge it there before creating a release"
    detail = (completed.stderr or completed.stdout).strip()
    return f"Git could not verify the repository's default branch: {detail or f'exit code {completed.returncode}'}"

def push(reports: list[ProjectPublishInfo], progress=None) -> list[dict]:
    gh = gh_path(); published = []
    for index, report in enumerate(reports, 1):
        if progress: progress(f"Preparing {report.name}, project {index} of {len(reports)}")
        root = Path(report.root)
        if not report.has_git:
            if progress: progress(f"Creating local Git repository for {report.name}")
            _run(["git", "init", "-b", "main"], root)
        push_remote = ""
        if report.remote:
            remotes = _repository_remotes(root)
            push_remote = next((name for name, url in remotes.items() if _remote_web_url(url).casefold() == _remote_web_url(report.remote).casefold()), "")
            if not push_remote:
                push_remote = "publisher"
                suffix = 2
                while push_remote in remotes:
                    push_remote = f"publisher{suffix}"; suffix += 1
                _git(root, "remote", "add", push_remote, report.remote)
        publishable_paths = _publishable_paths(root)
        if publishable_paths:
            _git(root, "add", "--all", "--", *publishable_paths)
        staged = subprocess.run(["git", "-C", str(root), "diff", "--cached", "--quiet"], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode != 0
        if staged:
            if progress: progress(f"Committing changes for {report.name}")
            _git(root, "commit", "-m", "Prepare NVDA add-on store release")
        remote = report.remote
        if not remote:
            if progress: progress(f"Finding or creating a public GitHub repository for {report.name}")
            repository = _repository_name(report.name)
            try:
                existing = _run([gh, "repo", "view", repository, "--json", "url", "--jq", ".url"])
                push_remote = "origin"
                _git(root, "remote", "add", push_remote, existing + ".git")
            except RuntimeError:
                _run([gh, "repo", "create", repository, "--public", "--source", str(root), "--remote", "origin", "--description", report.summary])
                push_remote = "origin"
            remote = _git(root, "remote", "get-url", push_remote)
        branch = _git(root, "branch", "--show-current") or "main"
        if progress: progress(f"Pushing {report.name} to GitHub")
        _git(root, "push", "--set-upstream", push_remote, branch)
        # Releases must target the commit we actually pushed.  GitHub otherwise
        # defaults to the repository's default branch, which may be unrelated.
        target_commit = _git(root, "rev-parse", "HEAD")
        repository = _remote_web_url(remote)
        default_branch = report.default_branch
        release_pending = release_needed(report.version, report.github_release_version)
        if release_pending:
            try:
                details = json.loads(_run([gh, "repo", "view", repository, "--json", "defaultBranchRef"]) or "{}")
                current_default_branch = str((details.get("defaultBranchRef") or {}).get("name") or "")
                if current_default_branch:
                    default_branch = current_default_branch
            except (RuntimeError, TypeError, ValueError):
                pass
        blocker = release_target_blocker(root, push_remote, default_branch, target_commit) if release_pending else ""
        published.append({**report.__dict__, "sourceUrl": repository, "targetCommitish": target_commit, "defaultBranch": default_branch, "releaseBlocker": blocker})
        if progress: progress(f"GitHub push finished for {report.name}, project {index} of {len(reports)}")
    return published

def _package_source(manifest: Path) -> Path:
    return manifest.parent

def build_package(item: dict, output_folder: Path) -> Path:
    manifest = Path(item["manifest"]); source = _package_source(manifest); output_folder.mkdir(parents=True, exist_ok=True)
    filename = f"{item['name']}-{item['version']}.nvda-addon"; output = output_folder / filename
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

def build_release_package(item: dict, output_folder: Path) -> Path:
    """Use a project's audited builder when it supplies one; otherwise use the safe generic builder."""
    source = _package_source(Path(item["manifest"]))
    build_script = source / "build.ps1"
    if not build_script.is_file():
        return build_package(item, output_folder)
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        raise RuntimeError(f"PowerShell is required to run the project builder for {item['name']}")
    outputs = source / "outputs"
    previous = {
        path.resolve(): (path.stat().st_mtime_ns, path.stat().st_size)
        for path in outputs.glob("*.nvda-addon")
    } if outputs.is_dir() else {}
    _run([shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(build_script), "-Version", str(item["version"])], cwd=source)
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

def validate_publish_builds(reports: list[ProjectPublishInfo], progress=None) -> None:
    """Prove releasable projects build successfully before changing GitHub."""
    pending = [report for report in reports if release_needed(report.version, report.github_release_version)]
    for index, report in enumerate(pending, 1):
        if progress:
            progress(f"Validating release build for {report.name}, project {index} of {len(pending)}")
        root = Path(report.root)
        build_release_package(report.__dict__, root / "outputs")

def release_create_arguments(gh: str, tag: str, package: Path, repository: str, target: str, release_name: str, prerelease: bool = False) -> list[str]:
    # Preflight compares the local version with the latest GitHub release, and
    # GitHub rejects duplicate tags.  Do not use --fail-on-no-commits here: gh
    # can falsely report no commits when --target is a SHA from a non-default
    # branch, and a version-only/package-only release is valid in any event.
    # Use fixed factual text. GitHub-generated notes can import unreviewed commit
    # messages into a public release after the submission audit has completed.
    arguments = [gh, "release", "create", tag, str(package), "--repo", repository, "--target", target, "--title", f"{release_name} {tag.removeprefix('v')}", "--notes", f"Release package for {release_name} {tag.removeprefix('v')}." ]
    if prerelease:
        arguments.append("--prerelease")
    return arguments

def release(items: list[dict], output_folder: Path, progress=None, on_released=None) -> list[dict]:
    gh = gh_path(); released = []
    for index, item in enumerate(items, 1):
        if item.get("releaseBlocker"):
            raise RuntimeError(f"Release creation was stopped for {item['name']}: {item['releaseBlocker']}")
        if progress: progress(f"Packaging {item['name']}, release {index} of {len(items)}")
        package = build_release_package(item, output_folder); tag = f"v{item['version']}"
        repository = item["sourceUrl"].removeprefix("https://github.com/")
        target = str(item.get("targetCommitish", "")).strip()
        if not target: raise RuntimeError(f"No pushed commit was recorded for {item['name']}; release creation was stopped")
        version = item["version"].lower()
        prerelease = item.get("channel") in {"beta", "dev"} or any(stage in version for stage in ("alpha", "beta", "dev", "rc"))
        arguments = release_create_arguments(gh, tag, package, repository, target, item.get("summary") or item["name"], prerelease)
        if progress: progress(f"Uploading GitHub Release {tag} for {item['name']}")
        _run(arguments)
        download = f"{item['sourceUrl']}/releases/download/{urllib.parse.quote(tag)}/{urllib.parse.quote(package.name)}"
        released_item = {**item, "package": str(package), "downloadUrl": download}
        released.append(released_item)
        # GitHub has already accepted this release.  Persist it before attempting
        # the next item so a later failure cannot make this success look missing.
        if on_released is not None: on_released(released_item)
        if progress: progress(f"GitHub Release published for {item['name']}, release {index} of {len(items)}")
    return released

def store_url(item: dict) -> str:
    version = item["version"].lower(); channel = item.get("channel") or ("dev" if "dev" in version or "alpha" in version else "beta" if "beta" in version or "rc" in version else "stable")
    submission_name = repository_name(item["sourceUrl"])
    fields = {
        "title": f"[Submit add-on]: {submission_name} {item['version']}", "download-url": item["downloadUrl"],
        "source-url": item["sourceUrl"], "publisher": STORE_PUBLISHER, "channel": channel,
        "license-name": "GPL v2", "license-url": "https://www.gnu.org/licenses/gpl-2.0.html",
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
