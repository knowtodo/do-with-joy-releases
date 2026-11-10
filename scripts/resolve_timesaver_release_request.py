from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

if __package__:
    from .timesaver_release_assets import AssetValidationError, parse_optional_repair_expectations
else:
    from timesaver_release_assets import AssetValidationError, parse_optional_repair_expectations

REQUIRED_REQUEST_KEYS = {
    "application",
    "version",
    "source_repository",
    "source_run_id",
    "artifact_name",
    "expected_source_commit",
    "expected_zip_sha256",
    "expected_exe_sha256",
    "expected_updater_sha256",
    "release_notes",
}
OPTIONAL_REPAIR_KEYS = {"expected_repair_sha256", "expected_repair_bytes"}
REQUEST_KEYS = REQUIRED_REQUEST_KEYS | OPTIONAL_REPAIR_KEYS
VERSION_PATTERN = r"(?:0|[1-9][0-9]{0,8})[.](?:0|[1-9][0-9]{0,8})[.](?:0|[1-9][0-9]{0,8})"
VERSION_RE = re.compile(VERSION_PATTERN)
RETRY_PATH_RE = re.compile(
    rf"release-requests/timesaver/retries/(?P<version>{VERSION_PATTERN})-(?P<number>[1-9][0-9]*)[.]json"
)
COMMIT_RE = re.compile(r"[0-9a-fA-F]{40}")
HASH_RE = re.compile(r"[0-9a-fA-F]{64}")
RUN_ID_RE = re.compile(r"[1-9][0-9]*")
OUTPUT_FIELDS = (
    "version",
    "source_repository",
    "source_run_id",
    "artifact_name",
    "expected_source_commit",
    "expected_zip_sha256",
    "expected_exe_sha256",
    "expected_updater_sha256",
    "release_notes",
    "expected_repair_sha256",
    "expected_repair_bytes",
)


class RequestError(ValueError):
    """Invalid or unsafe release request."""


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RequestError("Release request contains a duplicate JSON key.")
        result[key] = value
    return result


def validate_request(data: Any, request_path: str | None = None) -> dict[str, str]:
    if not isinstance(data, dict):
        raise RequestError("Release request fields do not match the required schema.")
    data_keys = set(data)
    if data_keys not in (REQUIRED_REQUEST_KEYS, REQUEST_KEYS):
        raise RequestError("Release request fields do not match the required schema.")
    if data["application"] != "timesaver":
        raise RequestError("Release request application must be timesaver.")

    version = data["version"]
    if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
        raise RequestError("Release request version must be X.Y.Z without leading zeroes.")
    if request_path is not None and request_path != f"release-requests/timesaver/{version}.json":
        raise RequestError("Release request path must match its version exactly.")

    if data["source_repository"] != "knowtodo/timesaver":
        raise RequestError("Release request source repository is not allowed.")
    run_id = data["source_run_id"]
    if not isinstance(run_id, str) or not RUN_ID_RE.fullmatch(run_id):
        raise RequestError("Release request source run ID must be a positive decimal string.")
    if data["artifact_name"] != f"TIMESAVER-{version}-RELEASE-CANDIDATE":
        raise RequestError("Release request artifact name does not match the requested version.")

    commit = data["expected_source_commit"]
    if not isinstance(commit, str) or not COMMIT_RE.fullmatch(commit):
        raise RequestError("Release request source commit must be a full SHA-1.")
    for key in ("expected_zip_sha256", "expected_exe_sha256", "expected_updater_sha256"):
        value = data[key]
        if not isinstance(value, str) or not HASH_RE.fullmatch(value):
            raise RequestError(f"Release request {key} must be a SHA-256 hex digest.")

    notes = data["release_notes"]
    if not isinstance(notes, str) or not notes.strip() or len(notes) > 4000 or chr(0) in notes:
        raise RequestError("Release notes must be non-empty text of at most 4000 characters.")
    try:
        repair = parse_optional_repair_expectations(
            data.get("expected_repair_sha256", ""),
            data.get("expected_repair_bytes", ""),
        )
    except AssetValidationError as exc:
        raise RequestError(str(exc)) from exc
    normalized = dict(data)
    normalized["expected_repair_sha256"] = repair[0] if repair else ""
    normalized["expected_repair_bytes"] = str(repair[1]) if repair else ""
    return normalized


def parse_push_changes(name_status: str) -> str:
    rows = [line for line in name_status.splitlines() if line]
    if len(rows) != 1:
        raise RequestError("A release push must change exactly one file.")
    parts = rows[0].split(chr(9), 1)
    if len(parts) != 2 or parts[0] != "A":
        raise RequestError("A release request must be a newly added file; edits and deletions are rejected.")
    path = parts[1]
    if chr(9) in path or not path.startswith("release-requests/timesaver/") or not path.endswith(".json"):
        raise RequestError("The only allowed push change is a TIMESAVER release request JSON.")
    return path


def validate_push_context(repository: str, ref: str, before: str, after: str) -> None:
    if repository != "knowtodo/do-with-joy-releases" or ref != "refs/heads/main":
        raise RequestError("Release request pushes are accepted only on the deployment repository main branch.")
    if not COMMIT_RE.fullmatch(before) or not COMMIT_RE.fullmatch(after) or before == "0" * 40:
        raise RequestError("Push event must have valid existing before and after commit SHAs.")
    if before == after:
        raise RequestError("Push event contains no new commit.")


def request_from_dispatch(values: dict[str, str]) -> dict[str, str]:
    data = {"application": "timesaver"}
    data.update(values)
    return validate_request(data)


def _decode_request_json(raw_json: str) -> dict[str, Any]:
    try:
        data = json.loads(raw_json, object_pairs_hook=_unique_object)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RequestError("Release request file is not valid UTF-8 JSON.") from exc
    if not isinstance(data, dict):
        raise RequestError("Release request JSON must be an object.")
    return data


def validate_retry_sequence(retry_path: str, prior_retry_paths: list[str]) -> None:
    match = RETRY_PATH_RE.fullmatch(retry_path)
    if match is None:
        raise RequestError("Retry request path must be retries/X.Y.Z-N.json.")
    version = match.group("version")
    attempt = int(match.group("number"))
    prior_numbers: list[int] = []
    for prior_path in prior_retry_paths:
        prior_match = RETRY_PATH_RE.fullmatch(prior_path)
        if prior_match is None:
            raise RequestError("An existing retry request has an invalid path.")
        if prior_match.group("version") == version:
            prior_numbers.append(int(prior_match.group("number")))
    if len(prior_numbers) != len(set(prior_numbers)):
        raise RequestError("Duplicate retry attempt numbers already exist.")
    if sorted(prior_numbers) != list(range(1, len(prior_numbers) + 1)):
        raise RequestError("Existing retry attempts are not sequential.")
    if attempt != len(prior_numbers) + 1:
        raise RequestError("Retry request must use the next unused sequential attempt number.")


def request_from_retry(
    retry_path: str,
    raw_json: str,
    *,
    original_request_json: str,
    prior_retry_paths: list[str],
) -> dict[str, Any]:
    match = RETRY_PATH_RE.fullmatch(retry_path)
    if match is None:
        raise RequestError("Retry request path must be retries/X.Y.Z-N.json.")
    retry_data = validate_request(_decode_request_json(raw_json))
    if retry_data["version"] != match.group("version"):
        raise RequestError("Retry request path version does not match its payload.")
    validate_retry_sequence(retry_path, prior_retry_paths)

    original_path = f"release-requests/timesaver/{match.group('version')}.json"
    original_data = request_from_push(original_path, original_request_json)
    if retry_data != original_data:
        raise RequestError("Retry request fields must exactly match the original deployment request.")
    return retry_data


def request_from_push(
    path: str,
    raw_json: str,
    *,
    original_request_json: str | None = None,
    prior_retry_paths: list[str] | None = None,
) -> dict[str, Any]:
    retry_match = RETRY_PATH_RE.fullmatch(path)
    if retry_match is not None:
        if original_request_json is None:
            original_path = f"release-requests/timesaver/{retry_match.group('version')}.json"
            try:
                original_request_json = Path(original_path).read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise RequestError("Original deployment request is missing or unreadable.") from exc
        return request_from_retry(
            path,
            raw_json,
            original_request_json=original_request_json,
            prior_retry_paths=prior_retry_paths or [],
        )
    return validate_request(_decode_request_json(raw_json), request_path=path)


def version_tuple(value: str) -> tuple[int, int, int]:
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise RequestError("Current or requested update version is not valid X.Y.Z.")
    return tuple(map(int, value.split(".")))


def request_from_resolved_environment() -> dict[str, str]:
    data = {"application": "timesaver"}
    for field in OUTPUT_FIELDS:
        data[field] = os.environ.get(f"RESOLVED_{field.upper()}", "")
    return validate_request(data)


def ensure_version_allowed(
    current_json: dict[str, Any],
    request: dict[str, str],
    *,
    run_attempt: str,
    retry_baseline_version: str | None = None,
) -> str:
    if not isinstance(current_json, dict):
        raise RequestError("Current public update JSON must be an object.")
    current_version = version_tuple(current_json.get("version"))
    requested_version = version_tuple(request["version"])
    if retry_baseline_version is not None:
        baseline_version = version_tuple(retry_baseline_version)
        if current_version != baseline_version:
            raise RequestError(
                "Retry is allowed only while the public version matches the original request baseline."
            )
        if baseline_version >= requested_version:
            raise RequestError("Retry baseline must be older than the requested version.")
    if current_version > requested_version:
        raise RequestError("Current public version is newer; refusing a downgrade.")

    expected = {
        "app_id": "timesaver",
        "version": request["version"],
        "download_url": (
            "https://github.com/knowtodo/do-with-joy-releases/releases/download/"
            f"timesaver-v{request['version']}/TIMESAVER-{request['version']}.zip"
        ),
        "sha256": request["expected_zip_sha256"].lower(),
        "release_notes": request["release_notes"],
    }
    if current_version == requested_version:
        if current_json != expected:
            raise RequestError("This version is already published with different metadata.")
        if run_attempt == "1":
            raise RequestError("This version is already public; a new deployment request is rejected.")
        return "already-current"
    return "new-version"


def _original_public_version_for_request(version: str) -> str:
    request_path = f"release-requests/timesaver/{version}.json"
    additions = subprocess.run(
        ["git", "log", "--diff-filter=A", "--format=%H", "--", request_path],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.splitlines()
    if len(additions) != 1:
        raise RequestError("Original deployment request must have exactly one addition commit.")
    parent = subprocess.run(
        ["git", "rev-parse", f"{additions[0]}^"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    raw = subprocess.run(
        ["git", "show", f"{parent}:TIMESAVER_update.json"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout
    try:
        prior_json = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError) as exc:
        raise RequestError("Could not read the public-version baseline from the original request parent.") from exc
    if not isinstance(prior_json, dict) or prior_json.get("app_id") != "timesaver":
        raise RequestError("Original request parent has an invalid TIMESAVER update JSON.")
    baseline = prior_json.get("version")
    version_tuple(baseline)
    if version_tuple(baseline) >= version_tuple(version):
        raise RequestError("Original request was not newer than its recorded public-version baseline.")
    return baseline


def _request_from_environment() -> tuple[dict[str, Any], bool, str]:
    event_name = os.environ.get("REQUEST_EVENT_NAME", "")
    if event_name == "workflow_dispatch":
        names = {
            "version": "DISPATCH_VERSION",
            "source_repository": "DISPATCH_SOURCE_REPOSITORY",
            "source_run_id": "DISPATCH_SOURCE_RUN_ID",
            "artifact_name": "DISPATCH_ARTIFACT_NAME",
            "expected_source_commit": "DISPATCH_EXPECTED_SOURCE_COMMIT",
            "expected_zip_sha256": "DISPATCH_EXPECTED_ZIP_SHA256",
            "expected_exe_sha256": "DISPATCH_EXPECTED_EXE_SHA256",
            "expected_updater_sha256": "DISPATCH_EXPECTED_UPDATER_SHA256",
            "release_notes": "DISPATCH_RELEASE_NOTES",
            "expected_repair_sha256": "DISPATCH_EXPECTED_REPAIR_SHA256",
            "expected_repair_bytes": "DISPATCH_EXPECTED_REPAIR_BYTES",
        }
        request = request_from_dispatch({key: os.environ.get(env_name, "") for key, env_name in names.items()})
        return request, False, ""

    if event_name != "push":
        raise RequestError("Only workflow_dispatch and main release-request pushes are accepted.")

    before = os.environ.get("REQUEST_BEFORE", "")
    after = os.environ.get("REQUEST_SHA", "")
    validate_push_context(
        os.environ.get("GITHUB_REPOSITORY", ""),
        os.environ.get("REQUEST_REF", ""),
        before,
        after,
    )
    result = subprocess.run(
        ["git", "diff", "--name-status", "--no-renames", before, after, "--"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    path = parse_push_changes(result.stdout)
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise RequestError("Release request file could not be read as UTF-8.") from exc

    if RETRY_PATH_RE.fullmatch(path):
        prior = subprocess.run(
            ["git", "ls-tree", "-r", "--name-only", before, "--", "release-requests/timesaver/retries"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        ).stdout.splitlines()
        request = request_from_push(path, raw, prior_retry_paths=prior)
        baseline = _original_public_version_for_request(request["version"])
        return request, True, baseline

    return request_from_push(path, raw), False, ""




def write_outputs(
    request: dict[str, str],
    output_path: str,
    *,
    is_retry: bool = False,
    retry_baseline_version: str = "",
) -> None:
    newline = chr(10)
    with open(output_path, "a", encoding="utf-8", newline="") as output:
        for key in OUTPUT_FIELDS:
            value = request[key]
            if key != "release_notes":
                if newline in value or chr(13) in value or chr(0) in value:
                    raise RequestError(f"Release request {key} contains an invalid line break.")
                output.write(f"{key}={value}{newline}")
                continue
            delimiter = f"NOTES_{uuid.uuid4().hex}"
            while delimiter in value:
                delimiter = f"NOTES_{uuid.uuid4().hex}"
            output.write(f"{key}<<{delimiter}{newline}{value}{newline}{delimiter}{newline}")
        output.write(f"retry_request={str(is_retry).lower()}{newline}")
        output.write(f"retry_baseline_version={retry_baseline_version}{newline}")


def check_public_version(path: str) -> str:
    try:
        current_json = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError) as exc:
        raise RequestError("Could not parse the current public update JSON.") from exc
    request = request_from_resolved_environment()
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    if not RUN_ID_RE.fullmatch(attempt):
        raise RequestError("GitHub Actions run attempt is invalid.")
    retry_flag = os.environ.get("RESOLVED_RETRY_REQUEST", "false").lower()
    retry_baseline = os.environ.get("RESOLVED_RETRY_BASELINE_VERSION", "")
    if retry_flag not in {"true", "false"}:
        raise RequestError("Retry request marker is invalid.")
    if retry_flag == "true" and not retry_baseline:
        raise RequestError("Retry request is missing its original public-version baseline.")
    if retry_flag == "false" and retry_baseline:
        raise RequestError("A normal deployment request must not contain a retry baseline.")
    return ensure_version_allowed(
        current_json,
        request,
        run_attempt=attempt,
        retry_baseline_version=retry_baseline if retry_flag == "true" else None,
    )


def main() -> int:
    try:
        if sys.argv[1:] and sys.argv[1] == "--check-public-version":
            if len(sys.argv) != 3:
                raise RequestError("Expected the current update JSON path.")
            status = check_public_version(sys.argv[2])
            print(f"PUBLIC_VERSION_PREFLIGHT={status}")
            return 0

        if len(sys.argv) != 1:
            raise RequestError("Unexpected command-line arguments.")
        request, is_retry, retry_baseline_version = _request_from_environment()
        output_path = os.environ.get("GITHUB_OUTPUT")
        if not output_path:
            raise RequestError("GitHub Actions output path is unavailable.")
        write_outputs(
            request,
            output_path,
            is_retry=is_retry,
            retry_baseline_version=retry_baseline_version,
        )
        request_kind = "retry" if is_retry else "release"
        print(f"Validated TIMESAVER {request['version']} {request_kind} request.")
        return 0
    except RequestError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"::error::Failed to inspect the release request commit ({type(exc).__name__}).", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
