import argparse
import base64
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO = "knowtodo/do-with-joy-releases"
UPDATER_SHA256 = "ceb28ed1e8dfde50fd1bd8511de9d05900d4f3c98339aa2d55cefbc6f49cd498"
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
GITSHA = re.compile(r"^[0-9a-fA-F]{40}$")
APPS = {
    "timesaver": {
        "repo": "knowtodo/timesaver",
        "workflow": ".github/workflows/build-timesaver-windows.yml",
        "artifact": "timesaver-{version}-candidate",
        "zip": "TIMESAVER-{version}.zip",
        "exe": "TIMESAVER.exe",
        "json": "TIMESAVER_update.json",
    },
    "infofinder": {
        "repo": "knowtodo/infofinder",
        "workflow": ".github/workflows/build-infofinder-windows.yml",
        "artifact": "infofinder-{version}-candidate",
        "zip": "InfoFinder-{version}.zip",
        "exe": "InfoFinder.exe",
        "json": "INFOFINDER_update.json",
    },
}


class ValidationError(ValueError):
    pass


def version_tuple(value):
    match = SEMVER.fullmatch(value or "")
    if not match:
        raise ValidationError("Version must be in X.Y.Z format.")
    return tuple(map(int, match.groups()))


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stream_hash(stream):
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    return digest.hexdigest()


def github_json(url, token=None, method="GET", payload=None):
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "do-with-joy-release-pipeline",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            content = response.read()
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"GitHub API request failed with HTTP {error.code}.") from None
    except urllib.error.URLError as error:
        raise RuntimeError(f"GitHub API request failed: {error.reason}.") from None
    if not content:
        return {}
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise RuntimeError("GitHub API returned invalid JSON.") from None


def validate_run_record(run, app, run_id):
    config = APPS[app]
    if str(run.get("id", "")) != str(run_id):
        raise ValidationError("Source run ID mismatch.")
    if run.get("event") != "workflow_dispatch":
        raise ValidationError("Source run must be manually dispatched.")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        raise ValidationError("Source candidate run must have completed successfully.")
    if run.get("head_branch") != "main":
        raise ValidationError("Source candidate must be built from main.")
    if (run.get("head_repository") or {}).get("full_name", "").lower() != config["repo"]:
        raise ValidationError("Source repository does not match the selected application.")
    if str(run.get("path", "")).split("@", 1)[0] != config["workflow"]:
        raise ValidationError("Source run did not use the approved candidate workflow.")
    source_sha = str(run.get("head_sha", ""))
    if not GITSHA.fullmatch(source_sha):
        raise ValidationError("Source run does not contain a full commit SHA.")
    return source_sha.lower()


def write_output(values):
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        raise ValidationError("GitHub Actions output file is unavailable.")
    with open(output_path, "a", encoding="utf-8", newline="\n") as output:
        for key, value in values.items():
            if "\n" in value or "\r" in value:
                raise ValidationError("Workflow output contains a line break.")
            output.write(f"{key}={value}\n")


def validate_candidate(directory, app, version, source_sha):
    config = APPS[app]
    version_tuple(version)
    if not GITSHA.fullmatch(source_sha):
        raise ValidationError("Source commit must be a full 40-character SHA.")
    root = Path(directory)
    expected_zip = config["zip"].format(version=version)
    if not root.is_dir():
        raise ValidationError("Candidate artifact directory is missing.")
    files = {item.name for item in root.iterdir() if item.is_file()}
    folders = [item for item in root.iterdir() if item.is_dir()]
    if files != {expected_zip, "build-verification.json"} or folders:
        raise ValidationError("Artifact must contain only the versioned ZIP and build-verification.json.")
    package = root / expected_zip
    try:
        manifest = json.loads((root / "build-verification.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValidationError("Candidate manifest is missing or invalid.") from None
    if not isinstance(manifest, dict):
        raise ValidationError("Candidate manifest must be a JSON object.")
    for key, expected in (("app_id", app), ("version", version), ("source_commit", source_sha.lower())):
        if manifest.get(key) != expected:
            raise ValidationError(f"Candidate manifest {key} does not match.")
    for key in ("runner_image", "python", "tcl_tk"):
        if not isinstance(manifest.get(key), str) or not manifest[key].strip():
            raise ValidationError(f"Candidate manifest is missing {key}.")
    if not isinstance(manifest.get("dependencies"), list) or not manifest["dependencies"]:
        raise ValidationError("Candidate dependency provenance is missing.")
    tests = manifest.get("test_summary", "")
    if not isinstance(tests, str) or not re.search(r"Ran\s+\d+\s+tests?", tests):
        raise ValidationError("Candidate test count is missing.")
    if not re.search(r"(?:^|\|)\s*OK(?:\s+\(skipped=\d+\))?\s*(?:\||$)", tests):
        raise ValidationError("Candidate test summary does not show success.")
    if not zipfile.is_zipfile(package):
        raise ValidationError("Candidate package is missing or invalid.")
    expected_entries = {config["exe"], "Updater.exe"}
    try:
        with zipfile.ZipFile(package) as archive:
            infos = archive.infolist()
            names = [item.filename for item in infos]
            if len(names) != 2 or set(names) != expected_entries:
                raise ValidationError("ZIP must contain exactly the app EXE and Updater.exe.")
            if len(manifest.get("zip_entries", [])) != 2 or set(manifest["zip_entries"]) != expected_entries:
                raise ValidationError("Manifest ZIP entries do not match the package.")
            if sum(item.file_size for item in infos) > 2 * 1024 * 1024 * 1024:
                raise ValidationError("ZIP uncompressed size exceeds the allowed limit.")
            if any((item.external_attr >> 16) & 0o170000 == 0o120000 for item in infos):
                raise ValidationError("ZIP contains a symbolic link.")
            if archive.testzip() is not None:
                raise ValidationError("ZIP CRC verification failed.")
            with archive.open(config["exe"]) as stream:
                exe_hash = stream_hash(stream)
            with archive.open("Updater.exe") as stream:
                updater_hash = stream_hash(stream)
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise ValidationError(f"ZIP cannot be safely read: {error}") from None
    for key in ("exe_sha256", "updater_sha256", "zip_sha256"):
        if not isinstance(manifest.get(key), str) or not SHA256.fullmatch(manifest[key]):
            raise ValidationError(f"Candidate manifest {key} is invalid.")
    if exe_hash.lower() != manifest["exe_sha256"].lower():
        raise ValidationError("App EXE hash differs from the manifest.")
    if updater_hash.lower() != UPDATER_SHA256:
        raise ValidationError("Updater.exe differs from the approved common binary.")
    if updater_hash.lower() != manifest["updater_sha256"].lower():
        raise ValidationError("Updater.exe hash differs from the manifest.")
    actual_zip_hash = file_hash(package)
    if actual_zip_hash.lower() != manifest["zip_sha256"].lower():
        raise ValidationError("ZIP hash differs from the manifest.")
    return {
        "version": version,
        "zip_path": str(package.resolve()),
        "zip_name": expected_zip,
        "zip_sha256": actual_zip_hash,
        "zip_size": package.stat().st_size,
    }


def normalized_notes(value):
    lines = []
    for raw in value.splitlines():
        line = re.sub(r"^(?:[-*•]\s*)+", "", raw.strip()).strip()
        if line:
            lines.append(line)
    if not lines:
        raise ValidationError("At least one non-empty release note is required.")
    return lines


def update_metadata_value(existing, app, version, digest, notes_text):
    if existing.get("app_id") != app:
        raise ValidationError("Existing update JSON app_id does not match.")
    if version_tuple(existing.get("version", "")) >= version_tuple(version):
        raise ValidationError("New version must be greater than the published update JSON version.")
    if not SHA256.fullmatch(digest):
        raise ValidationError("ZIP SHA-256 is invalid.")
    config = APPS[app]
    tag = f"{app}-v{version}"
    notes = normalized_notes(notes_text)
    result = dict(existing)
    result.update({
        "app_id": app,
        "version": version,
        "download_url": f"https://github.com/{REPO}/releases/download/{tag}/{config['zip'].format(version=version)}",
        "sha256": digest.lower(),
    })
    if app == "infofinder":
        old = existing.get("release_notes", "")
        if not isinstance(old, str):
            raise ValidationError("Existing INFOFINDER cumulative notes are invalid.")
        section = f"[v{version}]\n" + "\n".join(f"- {line}" for line in notes)
        result["release_notes"] = f"{old.rstrip()}\n{section}".lstrip()
        result["notes"] = notes
    else:
        result["release_notes"] = "\n".join(notes)
    return result


def api_url(path):
    return f"https://api.github.com/repos/{REPO}/{path.lstrip('/')}"


def verify_published_release(app, version, digest, public_zip, token):
    config = APPS[app]
    tag = f"{app}-v{version}"
    release = github_json(api_url(f"releases/tags/{tag}"), token)
    if release.get("tag_name") != tag or release.get("draft") or release.get("prerelease"):
        raise ValidationError("Release is not publicly published.")
    asset_name = config["zip"].format(version=version)
    assets = [item for item in release.get("assets", []) if item.get("name") == asset_name]
    if len(assets) != 1 or len(release.get("assets", [])) != 1:
        raise ValidationError("Release must contain only the expected ZIP.")
    expected_url = f"https://github.com/{REPO}/releases/download/{tag}/{asset_name}"
    if assets[0].get("browser_download_url") != expected_url:
        raise ValidationError("Public Release URL does not match the expected URL.")
    if assets[0].get("size") != public_zip.stat().st_size:
        raise ValidationError("Public Release asset size differs from the verified download.")
    if file_hash(public_zip).lower() != digest.lower():
        raise ValidationError("Downloaded public ZIP SHA-256 differs from the validated candidate.")


def publish_metadata(app, version, notes_text, public_zip, token):
    config = APPS[app]
    digest = file_hash(public_zip)
    tag = f"{app}-v{version}"
    asset_name = config["zip"].format(version=version)
    expected_url = f"https://github.com/{REPO}/releases/download/{tag}/{asset_name}"
    verify_published_release(app, version, digest, public_zip, token)
    path = config["json"]
    current = github_json(api_url(f"contents/{path}?ref=main"), token)
    try:
        existing = json.loads(base64.b64decode("".join(current["content"].split()), validate=True).decode("utf-8"))
    except (KeyError, ValueError, UnicodeError, json.JSONDecodeError):
        raise ValidationError("Current update JSON could not be decoded.") from None
    if not isinstance(existing, dict) or not isinstance(current.get("sha"), str):
        raise ValidationError("Current update JSON response is invalid.")
    result = update_metadata_value(existing, app, version, digest, notes_text)
    body = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    payload = {
        "message": f"chore(release): publish {app} {version} update metadata",
        "content": base64.b64encode(body).decode("ascii"),
        "sha": current["sha"],
        "branch": "main",
    }
    github_json(api_url(f"contents/{path}"), token, "PUT", payload)
    raw_url = f"https://raw.githubusercontent.com/{REPO}/main/{path}"
    for attempt in range(12):
        try:
            with urllib.request.urlopen(raw_url, timeout=30) as response:
                public = json.loads(response.read().decode("utf-8"))
            if (
                public.get("app_id") == app
                and public.get("version") == version
                and public.get("download_url") == expected_url
                and str(public.get("sha256", "")).lower() == digest.lower()
            ):
                print(f"Verified public {path} version {version}.")
                return
        except (urllib.error.URLError, UnicodeError, json.JSONDecodeError):
            pass
        if attempt < 11:
            time.sleep(5)
    raise RuntimeError(f"{path} was committed but public raw verification timed out; inspect before retrying.")


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    request = sub.add_parser("validate-request")
    request.add_argument("--app", choices=APPS, required=True)
    request.add_argument("--version", required=True)
    verify = sub.add_parser("verify-run")
    verify.add_argument("--app", choices=APPS, required=True)
    verify.add_argument("--version", required=True)
    verify.add_argument("--run-id", required=True)
    candidate = sub.add_parser("validate-candidate")
    candidate.add_argument("--app", choices=APPS, required=True)
    candidate.add_argument("--version", required=True)
    candidate.add_argument("--source-sha", required=True)
    candidate.add_argument("--artifact-dir", required=True)
    update = sub.add_parser("update-metadata")
    update.add_argument("--app", choices=APPS, required=True)
    update.add_argument("--version", required=True)
    update.add_argument("--public-zip", required=True)
    args = parser.parse_args()
    try:
        if args.command == "validate-request":
            if os.environ.get("GITHUB_REF") != "refs/heads/main":
                raise ValidationError("Release workflow must run from main.")
            if os.environ.get("APP_ID") != args.app or os.environ.get("APP_VERSION") != args.version:
                raise ValidationError("Publication request inputs do not match.")
            if os.environ.get("PUBLISH_CONFIRMATION") != f"PUBLISH {args.app} {args.version}":
                raise ValidationError("Explicit publication confirmation does not match.")
            normalized_notes(os.environ.get("RELEASE_NOTES", ""))
            requested_version = version_tuple(args.version)
            response = github_json(api_url(f"contents/{APPS[args.app]['json']}?ref=main"))
            try:
                current = json.loads(base64.b64decode("".join(response["content"].split()), validate=True).decode("utf-8"))
            except (KeyError, ValueError, UnicodeError, json.JSONDecodeError):
                raise ValidationError("Current public update JSON cannot be read; release is blocked.") from None
            if current.get("app_id") != args.app or requested_version <= version_tuple(current.get("version", "")):
                raise ValidationError("Requested version must be newer than the current public update JSON.")
            print(f"Validated manual publication request for {args.app} {args.version}.")
        elif args.command == "verify-run":
            version_tuple(args.version)
            if not args.run_id.isdecimal():
                raise ValidationError("Source run ID must contain digits only.")
            token = os.environ.get("SOURCE_TOKEN", "")
            if not token:
                raise ValidationError("Source Actions-read token is unavailable.")
            repo = APPS[args.app]["repo"]
            url = f"https://api.github.com/repos/{repo}/actions/runs/{args.run_id}"
            source_sha = validate_run_record(github_json(url, token), args.app, args.run_id)
            write_output({
                "source_sha": source_sha,
                "artifact_name": APPS[args.app]["artifact"].format(version=args.version),
            })
            print(f"Verified successful main candidate run for {args.app} {args.version}.")
        elif args.command == "validate-candidate":
            result = validate_candidate(args.artifact_dir, args.app, args.version, args.source_sha)
            write_output({key: str(value) for key, value in result.items()})
            print(f"Verified {result['zip_name']} ({result['zip_size']} bytes), SHA-256 {result['zip_sha256']}.")
        else:
            token = os.environ.get("GH_TOKEN", "")
            notes = os.environ.get("RELEASE_NOTES", "")
            if not token:
                raise ValidationError("Release-scoped Contents-write token is unavailable.")
            publish_metadata(args.app, args.version, notes, Path(args.public_zip), token)
    except (ValidationError, RuntimeError) as error:
        print(f"Release pipeline stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
