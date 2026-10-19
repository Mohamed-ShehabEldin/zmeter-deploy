"""One-click ZMeter release deployment and Conda environment setup.

This standard-library-only tool installs immutable assets from published
``lictailer/zmeter`` GitHub releases. It never launches ZMeter, imports device
modules, installs OS drivers, or accesses hardware. The optional local attoDRY
Python API is installed only with explicit operator consent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import TextIO


GITHUB_REPOSITORY = "lictailer/zmeter"
GITHUB_API_BASE = f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
GITHUB_API_VERSION = "2022-11-28"
NETWORK_TIMEOUT_SECONDS = 30
DOWNLOAD_CHUNK_SIZE = 128 * 1024
COPY_CHUNK_SIZE = 128 * 1024
DISK_SPACE_MARGIN = 256 * 1024 * 1024
MAX_RELEASE_PAGES = 10
MANIFEST_NAME = "deployment-manifest.json"
ATTO_DEVICE_VENDOR_WHEEL = "api_atto-device-python_1.0.12.whl"
ATTO_DEVICE_INSTALL_WHEEL = "atto_device-1.12.0-py3-none-any.whl"
ATTO_DEVICE_WHEEL_SHA256 = (
    "b1c0a7504e532105a5936bdd140054de7d1d2338b322bf12c21b94a7b9a8c5e6"
)
LEGACY_STABLE_TAG = "v0.10.0"
STABLE_TAG = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
BETA_TAG = re.compile(
    r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)-beta\.(0|[1-9][0-9]*)$"
)
COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
ASSET_DIGEST = re.compile(r"^sha256:([0-9a-f]{64})$", re.IGNORECASE)
INVALID_WINDOWS_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
RESERVED_WINDOWS_NAME = re.compile(
    r"^(CON|PRN|AUX|NUL|CLOCK\$|COM[1-9]|LPT[1-9])$", re.IGNORECASE
)
SAFE_DOWNLOAD_HOSTS = frozenset(
    {
        "api.github.com",
        "github.com",
        "objects.githubusercontent.com",
        "release-assets.githubusercontent.com",
    }
)


class UserCancelled(Exception):
    """The user cancelled before the selected operation completed."""


PreviewCancelled = UserCancelled  # Phase 1 import compatibility.


class CondaCommandError(RuntimeError):
    """A modifying Conda command returned a nonzero exit code."""

    def __init__(self, exit_code: int):
        self.exit_code = exit_code
        super().__init__(
            f"Conda exited with code {exit_code}. The environment may need review; "
            "it was not rolled back or deleted."
        )


class CondaInterrupted(RuntimeError):
    """Conda was interrupted after environment changes could have started."""


@dataclass(frozen=True)
class TagInfo:
    tag: str
    channel: str
    major: int
    minor: int
    beta_number: int | None
    normalized_key: str
    is_legacy: bool = False


@dataclass(frozen=True)
class ReleaseAsset:
    asset_id: int | None
    name: str
    size: int
    api_url: str | None
    digest: str | None


@dataclass(frozen=True)
class Release:
    tag: str
    channel: str
    major: int
    minor: int
    beta_number: int | None
    normalized_key: str
    published_at: datetime
    target_commitish: str
    asset: ReleaseAsset

    @property
    def asset_name(self) -> str:
        return self.asset.name

    @property
    def asset_size(self) -> int:
        return self.asset.size


@dataclass(frozen=True)
class DeploymentManifest:
    schema_version: int
    version: str
    tag: str
    channel: str
    commit: str
    environment_file: str
    environment_sha256: str


@dataclass(frozen=True)
class VerifiedPackage:
    archive_path: Path
    manifest: DeploymentManifest
    environment_path: Path
    members: tuple[zipfile.ZipInfo, ...]
    uncompressed_size: int


@dataclass(frozen=True)
class ExcludedRelease:
    tag: str
    reason: str


@dataclass(frozen=True)
class ReleaseChoices:
    valid: tuple[Release, ...]
    excluded: tuple[ExcludedRelease, ...]
    stable: tuple[Release, ...]
    beta: tuple[Release, ...]

    @property
    def newest_stable(self) -> Release | None:
        return self.stable[0] if self.stable else None

    @property
    def newest_beta(self) -> Release | None:
        return self.beta[0] if self.beta else None


@dataclass(frozen=True)
class EnvironmentState:
    name: str
    exists: bool

    @property
    def action(self) -> str:
        return "Update existing environment" if self.exists else "Create environment"


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Allow release downloads to redirect only to GitHub-owned HTTPS hosts."""

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        validate_download_url(new_url, allow_redirect_host=True)
        redirected = super().redirect_request(
            request, file_pointer, code, message, headers, new_url
        )
        if (
            redirected is not None
            and urllib.parse.urlsplit(request.full_url).hostname
            != urllib.parse.urlsplit(new_url).hostname
        ):
            redirected.remove_header("Authorization")
        return redirected


class GitHubClient:
    """Small read-only client for the fixed ZMeter GitHub repository."""

    def __init__(self, token=None, opener=None, timeout=NETWORK_TIMEOUT_SECONDS):
        self._token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self._opener = opener or urllib.request.build_opener(SafeRedirectHandler())
        self._timeout = timeout

    def _headers(self, accept="application/vnd.github+json"):
        headers = {
            "Accept": accept,
            "User-Agent": "zmeter-release-deployer",
            "X-GitHub-Api-Version": GITHUB_API_VERSION,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def _open(self, request):
        try:
            return self._opener.open(request, timeout=self._timeout)
        except urllib.error.HTTPError as error:
            if error.code in {401, 403}:
                raise ValueError(
                    "GitHub denied or rate-limited the request. Set GITHUB_TOKEN "
                    "to a read-only token and try again."
                ) from error
            if error.code == 404:
                raise FileNotFoundError("The requested GitHub release was not found.") from error
            raise ValueError(f"GitHub request failed with HTTP {error.code}.") from error
        except urllib.error.URLError as error:
            raise ValueError(f"Unable to reach GitHub: {error.reason}") from error
        except TimeoutError as error:
            raise ValueError("The GitHub request timed out. Try again safely.") from error

    def _get_json(self, url, allow_not_found=False):
        validate_repository_api_url(url)
        request = urllib.request.Request(url, headers=self._headers(), method="GET")
        try:
            with self._open(request) as response:
                payload = response.read()
        except FileNotFoundError:
            if allow_not_found:
                return None
            raise
        try:
            return json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("GitHub returned invalid JSON release metadata.") from error

    def list_releases(self):
        releases = []
        for page in range(1, MAX_RELEASE_PAGES + 1):
            document = self._get_json(
                f"{GITHUB_API_BASE}/releases?per_page=100&page={page}"
            )
            if not isinstance(document, list) or not all(
                isinstance(entry, dict) for entry in document
            ):
                raise ValueError("GitHub returned invalid release-list metadata.")
            releases.extend(document)
            if len(document) < 100:
                return releases
        raise ValueError("GitHub returned too many release pages to inspect safely.")

    def get_release_by_tag(self, tag):
        encoded_tag = urllib.parse.quote(tag, safe="")
        document = self._get_json(
            f"{GITHUB_API_BASE}/releases/tags/{encoded_tag}", allow_not_found=True
        )
        if document is not None and not isinstance(document, dict):
            raise ValueError("GitHub returned invalid exact-release metadata.")
        return document

    def resolve_tag_commit(self, tag):
        encoded_tag = urllib.parse.quote(tag, safe="")
        document = self._get_json(f"{GITHUB_API_BASE}/git/ref/tags/{encoded_tag}")
        if not isinstance(document, dict):
            raise ValueError(f"Unable to resolve tagged commit for '{tag}'.")
        git_object = document.get("object")
        for _ in range(5):
            if not isinstance(git_object, dict):
                break
            object_type = git_object.get("type")
            sha = git_object.get("sha")
            if object_type == "commit" and isinstance(sha, str):
                normalized = sha.casefold()
                if COMMIT_SHA.fullmatch(normalized):
                    return normalized
                break
            if object_type != "tag":
                break
            object_url = git_object.get("url")
            if not isinstance(object_url, str):
                break
            validate_tag_object_url(object_url)
            tag_document = self._get_json(object_url)
            if not isinstance(tag_document, dict):
                break
            git_object = tag_document.get("object")
        raise ValueError(f"Unable to resolve tagged commit for '{tag}'.")

    def open_asset(self, asset):
        if asset.api_url is None or asset.asset_id is None:
            raise ValueError("The selected release has no downloadable asset URL.")
        validate_asset_api_url(asset.api_url, asset.asset_id)
        request = urllib.request.Request(
            asset.api_url,
            headers=self._headers("application/octet-stream"),
            method="GET",
        )
        return self._open(request)


class ConsoleProgress:
    """Compact stage and in-place percentage output for the popup terminal."""

    def __init__(self, stream: TextIO | None = None, width=24):
        self.stream = stream or sys.stdout
        self.width = width
        self._last = None

    def stage(self, number, total, text):
        self.stream.write(f"\n[{number}/{total}] {text}\n")
        self.stream.flush()

    def update(self, label, current, total):
        percentage = 100 if total <= 0 else min(100, int(current * 100 / total))
        state = (label, percentage)
        if state == self._last and current < total:
            return
        self._last = state
        filled = self.width * percentage // 100
        bar = "#" * filled + "-" * (self.width - filled)
        self.stream.write(
            f"\r{label}: [{bar}] {percentage:3d}% "
            f"({format_size(current)} / {format_size(total)})"
        )
        if current >= total:
            self.stream.write("\n")
        self.stream.flush()


def default_catalog_path():
    return Path(__file__).resolve().with_name("releases.json")


def parse_tag(tag):
    if tag == LEGACY_STABLE_TAG:
        return TagInfo(tag, "stable", 0, 10, None, "stable:0.10", True)
    stable_match = STABLE_TAG.fullmatch(tag)
    if stable_match:
        major, minor = map(int, stable_match.groups())
        return TagInfo(tag, "stable", major, minor, None, f"stable:{major}.{minor}")
    beta_match = BETA_TAG.fullmatch(tag)
    if beta_match:
        major, minor, beta_number = map(int, beta_match.groups())
        return TagInfo(
            tag, "beta", major, minor, beta_number, f"beta:{major}.{minor}.{beta_number}"
        )
    return None


def validate_repository_api_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "api.github.com"
        or not parsed.path.startswith(f"/repos/{GITHUB_REPOSITORY}/")
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("GitHub returned a URL outside the allowed repository.")


def validate_asset_api_url(url, asset_id):
    validate_repository_api_url(url)
    parsed = urllib.parse.urlsplit(url)
    expected = f"/repos/{GITHUB_REPOSITORY}/releases/assets/{asset_id}"
    if parsed.path != expected or parsed.query or parsed.fragment:
        raise ValueError("GitHub returned an invalid release asset URL.")


def validate_tag_object_url(url):
    validate_repository_api_url(url)
    parsed = urllib.parse.urlsplit(url)
    prefix = f"/repos/{GITHUB_REPOSITORY}/git/tags/"
    if not parsed.path.startswith(prefix) or parsed.query or parsed.fragment:
        raise ValueError("GitHub returned an invalid annotated-tag URL.")


def validate_download_url(url, allow_redirect_host=False):
    parsed = urllib.parse.urlsplit(url)
    allowed = SAFE_DOWNLOAD_HOSTS if allow_redirect_host else {"api.github.com"}
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("GitHub redirected the download to an unsafe URL.")


def load_catalog(path):
    catalog_path = Path(path).resolve()
    if not catalog_path.is_file():
        raise ValueError(f"Release catalog was not found: {catalog_path}")
    try:
        with catalog_path.open("r", encoding="utf-8") as stream:
            document = json.load(stream)
    except json.JSONDecodeError as error:
        raise ValueError("Release catalog contains invalid JSON.") from error
    releases = document.get("releases") if isinstance(document, dict) else document
    if not isinstance(releases, list) or not all(isinstance(item, dict) for item in releases):
        raise ValueError("Release catalog must contain a list of release objects.")
    return releases


def _required_text(mapping, key, subject):
    value = mapping.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{subject} has invalid {key} metadata.")
    return value


def release_from_catalog_entry(entry, require_download=False):
    tag_value = entry.get("tag_name", "")
    tag = tag_value if isinstance(tag_value, str) else ""
    tag_info = parse_tag(tag)
    if tag_info is None:
        raise ValueError(f"Unsupported tag '{tag}'.")
    if entry.get("draft") is not False:
        raise ValueError(f"Release '{tag}' is a draft or has invalid draft metadata.")
    prerelease = entry.get("prerelease")
    if not isinstance(prerelease, bool):
        raise ValueError(f"Release '{tag}' has invalid prerelease metadata.")
    if tag_info.channel == "stable" and prerelease:
        raise ValueError(f"Stable tag '{tag}' is marked as a prerelease.")
    if tag_info.channel == "beta" and not prerelease:
        raise ValueError(f"Beta tag '{tag}' is not marked as a prerelease.")

    expected_asset = f"zmeter-{tag}-windows.zip"
    assets = entry.get("assets")
    matches = (
        [item for item in assets if isinstance(item, dict) and item.get("name") == expected_asset]
        if isinstance(assets, list)
        else []
    )
    if len(matches) != 1:
        raise ValueError(
            f"Release '{tag}' must contain exactly one asset named '{expected_asset}'."
        )
    asset_entry = matches[0]
    if asset_entry.get("state", "uploaded") != "uploaded":
        raise ValueError(f"Release '{tag}' asset is not fully uploaded.")
    asset_size = asset_entry.get("size")
    if isinstance(asset_size, bool) or not isinstance(asset_size, int) or asset_size <= 0:
        raise ValueError(f"Release '{tag}' has an invalid asset size.")
    asset_id_value = asset_entry.get("id")
    asset_id = (
        asset_id_value
        if isinstance(asset_id_value, int)
        and not isinstance(asset_id_value, bool)
        and asset_id_value > 0
        else None
    )
    api_url_value = asset_entry.get("url")
    api_url = api_url_value if isinstance(api_url_value, str) else None
    if require_download and (asset_id is None or api_url is None):
        raise ValueError(f"Release '{tag}' has incomplete download metadata.")
    if asset_id is not None and api_url is not None:
        validate_asset_api_url(api_url, asset_id)
    digest_value = asset_entry.get("digest")
    digest = None
    if digest_value is not None:
        digest_match = ASSET_DIGEST.fullmatch(digest_value) if isinstance(digest_value, str) else None
        if digest_match is None:
            raise ValueError(f"Release '{tag}' has an invalid asset digest.")
        digest = digest_match.group(1).casefold()

    published_value = entry.get("published_at")
    if not isinstance(published_value, str):
        raise ValueError(f"Release '{tag}' has an invalid publication date.")
    try:
        published_at = datetime.fromisoformat(published_value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"Release '{tag}' has an invalid publication date.") from error
    target_commitish = entry.get("target_commitish", "")
    if not isinstance(target_commitish, str):
        target_commitish = ""
    return Release(
        tag,
        tag_info.channel,
        tag_info.major,
        tag_info.minor,
        tag_info.beta_number,
        tag_info.normalized_key,
        published_at,
        target_commitish,
        ReleaseAsset(asset_id, expected_asset, asset_size, api_url, digest),
    )


def resolve_release_choices(entries, require_download=False):
    valid = []
    excluded = []
    for entry in entries:
        try:
            valid.append(release_from_catalog_entry(entry, require_download))
        except ValueError as error:
            excluded.append(ExcludedRelease(str(entry.get("tag_name", "")), str(error)))
    normalized = {}
    for release in valid:
        normalized.setdefault(release.normalized_key, []).append(release.tag)
    collision = next((tags for tags in normalized.values() if len(tags) > 1), None)
    if collision:
        raise ValueError(
            "Ambiguous releases normalize to the same version: " + ", ".join(collision)
        )
    stable = sorted(
        (release for release in valid if release.channel == "stable"),
        key=lambda release: (release.major, release.minor),
        reverse=True,
    )
    beta = sorted(
        (release for release in valid if release.channel == "beta"),
        key=lambda release: (
            release.major,
            release.minor,
            release.beta_number if release.beta_number is not None else -1,
        ),
        reverse=True,
    )
    return ReleaseChoices(tuple(valid), tuple(excluded), tuple(stable), tuple(beta))


def find_release(choices, tag):
    return next((release for release in choices.valid if release.tag == tag), None)


def format_size(byte_count):
    for divisor, suffix in ((1 << 30, "GB"), (1 << 20, "MB"), (1 << 10, "KB")):
        if byte_count >= divisor:
            return f"{byte_count / divisor:.1f} {suffix}"
    return f"{byte_count} B"


def _read(prompt):
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt) as error:
        raise UserCancelled from error


def select_operation(preview=False):
    print("ZMeter Setup" + (" - Preview" if preview else ""))
    print("\n1. Install ZMeter")
    print("2. Set up Python environment")
    while True:
        answer = _read("\nSelect an operation: ")
        if answer == "1":
            return "install"
        if answer == "2":
            return "environment"
        print("Enter 1 or 2.")


def _release_label(release):
    return release.tag if release is not None else "unavailable"


def select_release(choices, exact_lookup=None):
    print(f"\n1. Stable - {_release_label(choices.newest_stable)}")
    print(f"2. Beta - {_release_label(choices.newest_beta)}")
    print("3. Specific version")
    while True:
        answer = _read("\nSelect a version: ")
        if answer == "1":
            if choices.newest_stable is not None:
                return choices.newest_stable
            print("No supported stable release is available.")
        elif answer == "2":
            if choices.newest_beta is not None:
                return choices.newest_beta
            print("No supported beta release is available.")
        elif answer == "3":
            tag = _read("Published release tag: ")
            if parse_tag(tag) is None:
                print("Use vMAJOR.MINOR, vMAJOR.MINOR-beta.NUMBER, or v0.10.0.")
                continue
            release = find_release(choices, tag)
            if release is None and exact_lookup is not None:
                release = exact_lookup(tag)
            if release is not None:
                collision = next(
                    (
                        item
                        for item in choices.valid
                        if item.normalized_key == release.normalized_key and item.tag != release.tag
                    ),
                    None,
                )
                if collision is not None:
                    raise ValueError(
                        "Ambiguous releases normalize to the same version: "
                        f"{collision.tag}, {release.tag}"
                    )
                return release
            print(f"Published release '{tag}' is unavailable.")
        else:
            print("Enter 1, 2, or 3.")


def _path_key(path):
    return os.path.normcase(os.path.abspath(path))


def resolve_destination(value, development_checkout):
    if not value.strip():
        raise ValueError("Enter an installation folder.")
    destination = Path(value).expanduser()
    if not destination.is_absolute():
        raise ValueError("Enter a complete Windows path, such as D:\\ZMeter.")
    destination = destination.resolve(strict=False)
    if destination.exists():
        raise ValueError("That installation folder already exists.")
    if not destination.parent.is_dir():
        raise ValueError("The parent folder does not exist.")
    name = destination.name
    reserved_base = name.split(".", 1)[0]
    if (
        not name
        or name in {".", ".."}
        or INVALID_WINDOWS_NAME.search(name)
        or name.endswith((".", " "))
        or RESERVED_WINDOWS_NAME.fullmatch(reserved_base)
    ):
        raise ValueError("Choose a valid Windows installation folder name.")
    destination_key = _path_key(destination)
    checkout_key = _path_key(Path(development_checkout).resolve(strict=False))
    try:
        common = os.path.commonpath((destination_key, checkout_key))
    except ValueError:
        common = ""
    if common in {destination_key, checkout_key}:
        raise ValueError("The installation folder must not overlap this checkout.")
    return destination


def required_preflight_space(asset_size):
    return asset_size * 2 + DISK_SPACE_MARGIN


def check_download_space(directory, asset_size, disk_usage=shutil.disk_usage):
    required = asset_size + DISK_SPACE_MARGIN
    if disk_usage(directory).free < required:
        raise ValueError(
            f"At least {format_size(required)} is required for the temporary download."
        )


def check_destination_preflight(destination, asset_size, disk_usage=shutil.disk_usage):
    if destination.exists():
        raise ValueError("That installation folder already exists.")
    if not destination.parent.is_dir() or not os.access(destination.parent, os.W_OK):
        raise ValueError("The installation parent is not writable.")
    required = required_preflight_space(asset_size)
    if disk_usage(destination.parent).free < required:
        raise ValueError(
            f"At least {format_size(required)} of free space is required before download."
        )


def read_destination(development_checkout, asset_size=1):
    while True:
        value = _read("\nInstallation folder: ")
        try:
            destination = resolve_destination(value, development_checkout)
            check_destination_preflight(destination, asset_size)
            return destination
        except ValueError as error:
            print(error)


def get_environment_state(release, existing_environment_names=None):
    name = f"zmeter-{release.tag}"
    existing = {value.casefold() for value in (existing_environment_names or ())}
    return EnvironmentState(name, name.casefold() in existing)


def find_conda_executable(environ=None, which=shutil.which, home=None):
    environment = os.environ if environ is None else environ
    candidates = []
    configured = environment.get("CONDA_EXE")
    if configured:
        candidates.append(Path(configured))
    discovered = which("conda")
    if discovered:
        candidates.append(Path(discovered))
    user_home = home or Path.home()
    local_app_data = Path(environment.get("LOCALAPPDATA", user_home / "AppData" / "Local"))
    program_data = Path(environment.get("ProgramData", "C:/ProgramData"))
    for root in (
        user_home / "anaconda3",
        user_home / "miniconda3",
        local_app_data / "anaconda3",
        local_app_data / "miniconda3",
        program_data / "anaconda3",
        program_data / "miniconda3",
    ):
        candidates.append(root / "Scripts" / "conda.exe")
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ValueError(
        "Conda was not found. Install Anaconda, Miniconda, or Miniforge and try again."
    )


def list_conda_environment_names(conda_executable, runner=subprocess.run):
    result = runner(
        [str(conda_executable), "env", "list", "--json"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise ValueError(
            "Unable to read Conda environments." + (f" {detail}" if detail else "")
        )
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("Conda returned invalid environment information.") from error
    prefixes = document.get("envs") if isinstance(document, dict) else None
    if not isinstance(prefixes, list) or not all(isinstance(item, str) for item in prefixes):
        raise ValueError("Conda returned invalid environment information.")
    return tuple(dict.fromkeys(Path(prefix).name for prefix in prefixes if Path(prefix).name))


def format_summary(operation, release, destination=None, environment=None, preview=False):
    if operation == "install":
        if destination is None:
            raise ValueError("An installation destination is required.")
        lines = ["Install ZMeter", f"Version: {release.tag}", f"Location: {destination}"]
    elif operation == "environment":
        if environment is None:
            raise ValueError("Environment state is required.")
        lines = [
            "Set up Python environment",
            f"Version: {release.tag}",
            f"Environment: {environment.name}",
            f"Action: {environment.action}",
        ]
    else:
        raise ValueError(f"Unsupported operation: {operation}")
    lines.append(
        "Preview only - no changes will be made."
        if preview
        else "This action will make changes after confirmation."
    )
    return "\n".join(lines)


def confirm():
    return _read("\nContinue? [Y/N]: ").casefold() == "y"


def download_release_asset(client, release, destination, progress=None):
    digest = hashlib.sha256()
    received = 0
    try:
        with client.open_asset(release.asset) as response, destination.open("xb") as stream:
            while True:
                chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                if not chunk:
                    break
                stream.write(chunk)
                digest.update(chunk)
                received += len(chunk)
                if received > release.asset.size:
                    raise ValueError("The release download is larger than GitHub reported.")
                if progress is not None:
                    progress("Download", received, release.asset.size)
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    if received != release.asset.size:
        destination.unlink(missing_ok=True)
        raise ValueError("The release download size does not match GitHub metadata.")
    actual_digest = digest.hexdigest()
    if release.asset.digest is not None and actual_digest != release.asset.digest:
        destination.unlink(missing_ok=True)
        raise ValueError("The release download failed SHA-256 verification.")
    return actual_digest


def _safe_member_name(info):
    name = info.filename
    if not name or "\\" in name or name.startswith(("/", "//")):
        raise ValueError(f"Unsafe archive path: {name!r}")
    if re.match(r"^[A-Za-z]:", name):
        raise ValueError(f"Unsafe archive path: {name!r}")
    raw_parts = name.rstrip("/").split("/")
    pure = PurePosixPath(*raw_parts)
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError(f"Unsafe archive path: {name!r}")
    for part in raw_parts:
        reserved_base = part.split(".", 1)[0]
        if (
            INVALID_WINDOWS_NAME.search(part)
            or part.endswith((".", " "))
            or RESERVED_WINDOWS_NAME.fullmatch(reserved_base)
        ):
            raise ValueError(f"Unsafe Windows archive name: {name!r}")
    if any(part.casefold() == ".git" for part in pure.parts):
        raise ValueError("Release archives must not contain a .git directory.")
    unix_mode = (info.external_attr >> 16) & 0xFFFF
    file_type = stat.S_IFMT(unix_mode)
    if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
        raise ValueError(f"Archive links or special entries are not allowed: {name}")
    if info.create_system == 0 and (info.external_attr & 0x400):
        raise ValueError(f"Archive reparse-point entries are not allowed: {name}")
    if info.flag_bits & 0x1:
        raise ValueError(f"Encrypted archive entries are not supported: {name}")
    return pure.as_posix()


def inspect_archive(archive):
    members = tuple(archive.infolist())
    if not members:
        raise ValueError("The release archive is empty.")
    names = set()
    for info in members:
        canonical = _safe_member_name(info).casefold()
        if canonical in names:
            raise ValueError(f"Duplicate archive entry: {info.filename}")
        names.add(canonical)
    manifests = [
        info for info in members if _safe_member_name(info) == MANIFEST_NAME and not info.is_dir()
    ]
    if len(manifests) != 1:
        raise ValueError(f"The archive must contain exactly one root {MANIFEST_NAME}.")
    corrupt = archive.testzip()
    if corrupt is not None:
        raise ValueError(f"The release archive contains a corrupt entry: {corrupt}")
    return members, sum(info.file_size for info in members if not info.is_dir())


def parse_manifest(data, release, tagged_commit):
    if len(data) > 1024 * 1024:
        raise ValueError("The deployment manifest is unexpectedly large.")
    try:
        document = json.loads(data.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("The deployment manifest contains invalid JSON.") from error
    if not isinstance(document, dict):
        raise ValueError("The deployment manifest must be a JSON object.")
    subject = "The deployment manifest"
    version = _required_text(document, "version", subject)
    tag = _required_text(document, "tag", subject)
    channel = _required_text(document, "channel", subject)
    commit = _required_text(document, "commit", subject).casefold()
    environment_file = _required_text(document, "environment_file", subject)
    environment_sha256 = _required_text(document, "environment_sha256", subject).casefold()
    if (
        document.get("schema_version") != 1
        or tag != release.tag
        or version != release.tag[1:]
        or channel != release.channel
        or not COMMIT_SHA.fullmatch(commit)
        or commit != tagged_commit.casefold()
        or Path(environment_file).name != environment_file
        or "/" in environment_file
        or "\\" in environment_file
        or Path(environment_file).suffix.casefold() not in {".yml", ".yaml"}
        or environment_file.casefold()
        in {MANIFEST_NAME.casefold(), release.asset.name.casefold()}
        or not SHA256.fullmatch(environment_sha256)
    ):
        raise ValueError("The deployment manifest does not match the selected release.")
    return DeploymentManifest(
        1, version, tag, channel, commit, environment_file, environment_sha256
    )


def verify_release_package(archive_path, release, tagged_commit, work_directory):
    try:
        with zipfile.ZipFile(archive_path, "r") as archive:
            members, uncompressed_size = inspect_archive(archive)
            manifest_info = next(
                info for info in members if _safe_member_name(info) == MANIFEST_NAME
            )
            manifest = parse_manifest(archive.read(manifest_info), release, tagged_commit)
            environments = [
                info
                for info in members
                if _safe_member_name(info) == manifest.environment_file and not info.is_dir()
            ]
            if len(environments) != 1:
                raise ValueError("The archive does not contain the manifest's environment file.")
            environment_data = archive.read(environments[0])
    except zipfile.BadZipFile as error:
        raise ValueError("The downloaded release is not a valid ZIP archive.") from error
    if hashlib.sha256(environment_data).hexdigest() != manifest.environment_sha256:
        raise ValueError("The environment file failed SHA-256 verification.")
    environment_path = work_directory / manifest.environment_file
    environment_path.write_bytes(environment_data)
    return VerifiedPackage(
        archive_path, manifest, environment_path, members, uncompressed_size
    )


def _ensure_within(root, target):
    try:
        target.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError as error:
        raise ValueError("An archive entry would escape the staging directory.") from error


def _release_for_manifest(manifest):
    tag_info = parse_tag(manifest.tag)
    if tag_info is None:
        raise ValueError("The staged deployment manifest has an unsupported tag.")
    return Release(
        manifest.tag,
        manifest.channel,
        tag_info.major,
        tag_info.minor,
        tag_info.beta_number,
        tag_info.normalized_key,
        datetime.min,
        "",
        ReleaseAsset(None, "", 1, None, None),
    )


def extract_verified_package(
    package, destination, progress=None, disk_usage=shutil.disk_usage
):
    check_destination_preflight(destination, package.archive_path.stat().st_size, disk_usage)
    required = package.uncompressed_size + DISK_SPACE_MARGIN
    if disk_usage(destination.parent).free < required:
        raise ValueError(f"At least {format_size(required)} is required to extract ZMeter.")
    staging = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.zmeter-stage-", dir=destination.parent)
    )
    completed = 0
    try:
        with zipfile.ZipFile(package.archive_path, "r") as archive:
            for info in package.members:
                relative_name = _safe_member_name(info)
                target = staging.joinpath(*PurePosixPath(relative_name).parts)
                _ensure_within(staging, target)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    _ensure_within(staging, target)
                    if not target.is_dir():
                        raise ValueError(f"Unable to create archive directory: {relative_name}")
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                _ensure_within(staging, target)
                with archive.open(info, "r") as source, target.open("xb") as output:
                    while True:
                        chunk = source.read(COPY_CHUNK_SIZE)
                        if not chunk:
                            break
                        output.write(chunk)
                        completed += len(chunk)
                        if progress is not None:
                            progress("Extract", completed, package.uncompressed_size)
        parse_manifest(
            (staging / MANIFEST_NAME).read_bytes(),
            _release_for_manifest(package.manifest),
            package.manifest.commit,
        )
        staged_environment = staging / package.manifest.environment_file
        if hashlib.sha256(staged_environment.read_bytes()).hexdigest() != package.manifest.environment_sha256:
            raise ValueError("The staged environment file failed verification.")
        if destination.exists():
            raise ValueError("The installation folder appeared during deployment; nothing was replaced.")
        staging.rename(destination)
        staging = None
    except BaseException:
        if staging is not None and staging.exists():
            shutil.rmtree(staging)
        raise


def build_conda_command(conda_executable, environment, environment_file):
    command = [
        str(conda_executable),
        "env",
        "update" if environment.exists else "create",
        "--name",
        environment.name,
        "--file",
        str(environment_file),
    ]
    if environment.exists:
        command.append("--prune")
    return tuple(command)


def build_conda_pip_install_command(conda_executable, environment, wheel_path):
    return (
        str(conda_executable),
        "run",
        "--name",
        environment.name,
        "python",
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-index",
        "--no-deps",
        str(wheel_path),
    )


def _file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(COPY_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def _package_contains_attodry(package):
    return any(
        _safe_member_name(info).casefold().startswith("devices/attodry/")
        for info in package.members
    )


def find_attodry_vendor_wheel(installer_directory):
    installer_directory = Path(installer_directory).resolve()
    roots = (installer_directory, installer_directory.parent)
    relative_candidates = (
        Path(ATTO_DEVICE_VENDOR_WHEEL),
        Path(ATTO_DEVICE_INSTALL_WHEEL),
        Path("Attocube")
        / "u drive"
        / "Software"
        / "API"
        / ATTO_DEVICE_VENDOR_WHEEL,
        Path("Attocube")
        / "u drive"
        / "Software"
        / "API"
        / ATTO_DEVICE_INSTALL_WHEEL,
    )
    candidates = tuple(
        dict.fromkeys(root / relative for root in roots for relative in relative_candidates)
    )
    for candidate in candidates:
        if not candidate.is_file():
            continue
        if _file_sha256(candidate) != ATTO_DEVICE_WHEEL_SHA256:
            raise ValueError(
                "The local attoDRY Python wheel failed SHA-256 verification: "
                f"{candidate}"
            )
        return candidate
    searched = "\n".join(f"  - {candidate}" for candidate in candidates)
    raise FileNotFoundError(
        "This ZMeter release includes the attoDRY driver, but its vendor-supplied "
        "Python wheel was not found. Copy api_atto-device-python_1.0.12.whl "
        "beside deploy_zmeter.py or keep the Attocube/u drive folder beside it. "
        f"Searched:\n{searched}"
    )


def stage_attodry_vendor_wheel(source, work_directory):
    destination = Path(work_directory) / ATTO_DEVICE_INSTALL_WHEEL
    shutil.copyfile(source, destination)
    if _file_sha256(destination) != ATTO_DEVICE_WHEEL_SHA256:
        destination.unlink(missing_ok=True)
        raise ValueError("The staged attoDRY Python wheel failed SHA-256 verification.")
    return destination


def select_attodry_vendor_wheel(installer_directory):
    """Opt-in before Conda mutation; a missing optional API never blocks others."""
    print("\nThis release supports the optional attoDRY2100 device.")
    print("Its vendor Python API is not needed for other devices.")
    answer = _read("Will you use attoDRY and install its Python API? [y/N]: ")
    if answer.strip().casefold() not in {"y", "yes"}:
        print("Skipping attoDRY API. You can rerun environment setup to add it later.")
        return None
    try:
        return find_attodry_vendor_wheel(installer_directory)
    except FileNotFoundError:
        print("Use the Python wheel supplied by attocube with your controller.")
        supplied = _read("Full path to the vendor .whl (Enter to cancel setup): ").strip().strip('"')
        if not supplied:
            raise UserCancelled
        source = Path(supplied).expanduser()
        if not source.is_file() or _file_sha256(source) != ATTO_DEVICE_WHEEL_SHA256:
            raise ValueError("The selected attoDRY Python wheel is missing or failed SHA-256 verification.")
        return source


def run_conda_command(command, runner=subprocess.run, heading="Conda output"):
    print(f"\n{heading}:\n")
    try:
        result = runner(list(command), check=False)
    except KeyboardInterrupt as error:
        raise CondaInterrupted(
            "Conda was interrupted. The environment may be partially modified and needs review."
        ) from error
    if result.returncode != 0:
        raise CondaCommandError(result.returncode)


def execute_installation(client, release, destination, progress=None):
    display = progress or ConsoleProgress()
    with tempfile.TemporaryDirectory(prefix="zmeter-deploy-") as temporary:
        work_directory = Path(temporary)
        archive_path = work_directory / release.asset.name
        check_download_space(work_directory, release.asset.size)
        display.stage(1, 4, "Downloading release")
        download_release_asset(client, release, archive_path, display.update)
        display.stage(2, 4, "Verifying package")
        tagged_commit = client.resolve_tag_commit(release.tag)
        package = verify_release_package(archive_path, release, tagged_commit, work_directory)
        display.stage(3, 4, "Extracting ZMeter")
        extract_verified_package(package, destination, display.update)
        display.stage(4, 4, "Finished")
    print(f"\nZMeter {release.tag} was installed at:\n{destination}")


def execute_environment_setup(
    client,
    release,
    conda_executable,
    environment,
    progress=None,
    runner=subprocess.run,
    installer_directory=None,
):
    display = progress or ConsoleProgress()
    installer_directory = installer_directory or Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory(prefix="zmeter-environment-") as temporary:
        work_directory = Path(temporary)
        archive_path = work_directory / release.asset.name
        check_download_space(work_directory, release.asset.size)
        display.stage(1, 4, "Downloading release")
        download_release_asset(client, release, archive_path, display.update)
        display.stage(2, 4, "Verifying environment")
        tagged_commit = client.resolve_tag_commit(release.tag)
        package = verify_release_package(archive_path, release, tagged_commit, work_directory)
        attodry_wheel = None
        if _package_contains_attodry(package):
            source_wheel = select_attodry_vendor_wheel(installer_directory)
            if source_wheel is not None:
                attodry_wheel = stage_attodry_vendor_wheel(source_wheel, work_directory)
        display.stage(3, 4, "Running Conda")
        command = build_conda_command(conda_executable, environment, package.environment_path)
        run_conda_command(command, runner)
        if attodry_wheel is not None:
            display.stage(4, 4, "Installing verified attoDRY Python API")
            command = build_conda_pip_install_command(
                conda_executable, environment, attodry_wheel
            )
            run_conda_command(command, runner, "attoDRY package installation output")
        else:
            display.stage(4, 4, "Finished")
    print(f"\nEnvironment {environment.name} is ready.")


def _exact_release_lookup(client, tag):
    entry = client.get_release_by_tag(tag)
    if entry is None:
        return None
    try:
        return release_from_catalog_entry(entry, require_download=True)
    except ValueError:
        return None


def main(
    catalog_path=None,
    existing_environment_names=None,
    preview=False,
    github_client=None,
    conda_runner=subprocess.run,
):
    fixture_preview = catalog_path is not None
    preview = preview or fixture_preview
    client = github_client or GitHubClient()
    print("Checking published releases...")
    if fixture_preview:
        choices = resolve_release_choices(load_catalog(Path(catalog_path)))
        exact_lookup = None
    else:
        choices = resolve_release_choices(client.list_releases(), require_download=True)
        exact_lookup = lambda tag: _exact_release_lookup(client, tag)
    if not choices.valid:
        raise ValueError("No supported packaged releases are available yet.")

    operation = select_operation(preview)
    release = select_release(choices, exact_lookup)
    destination = None
    environment = None
    conda_executable = None
    if operation == "install":
        destination = read_destination(Path(__file__).resolve().parent, release.asset.size)
    else:
        if existing_environment_names is None:
            conda_executable = find_conda_executable()
            existing_environment_names = list_conda_environment_names(
                conda_executable, conda_runner
            )
        elif not preview:
            conda_executable = find_conda_executable()
        environment = get_environment_state(release, existing_environment_names)

    print("\n" + format_summary(operation, release, destination, environment, preview))
    if not confirm():
        raise UserCancelled
    if preview:
        print("\nPreview complete. No changes were made.")
        return
    if operation == "install":
        execute_installation(client, release, destination)
    else:
        execute_environment_setup(
            client, release, conda_executable, environment, runner=conda_runner
        )


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description="Install a verified ZMeter release.")
    parser.add_argument(
        "--preview", action="store_true", help="show the live operation without making changes"
    )
    parser.add_argument(
        "--catalog", type=Path, help="use a local fixture catalog; always forces preview mode"
    )
    return parser.parse_args(list(argv))


def run_clicked(argv=None):
    exit_code = 0
    try:
        arguments = parse_arguments(sys.argv[1:] if argv is None else argv)
        main(arguments.catalog, preview=arguments.preview)
    except UserCancelled:
        print("\nCancelled. No completed changes were made.")
    except CondaCommandError as error:
        exit_code = error.exit_code if 0 < error.exit_code < 256 else 1
        print(f"\nUnable to complete environment setup: {error}")
    except CondaInterrupted as error:
        exit_code = 130
        print(f"\nEnvironment setup interrupted: {error}")
    except KeyboardInterrupt:
        exit_code = 130
        print("\nCancelled. Temporary files were removed.")
    except Exception as error:
        exit_code = 1
        print(f"\nUnable to continue: {error}")
    finally:
        try:
            input("\nPress Enter to close.")
        except (EOFError, KeyboardInterrupt):
            pass
    return exit_code


if __name__ == "__main__":
    raise SystemExit(run_clicked())
