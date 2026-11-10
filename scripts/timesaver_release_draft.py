from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping

API_ROOT = "https://api.github.com/repos"
MAX_RELEASE_GET_ATTEMPTS = 3
TRANSIENT_HTTP_STATUSES = {404, 429, 500, 502, 503, 504}
SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class ReleaseResolutionError(ValueError):
    """The existing or newly created Release is unsafe to reuse."""


class GitHubAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


ApiCall = Callable[[str, str, Mapping[str, Any] | None], Any]


def normalize_notes(value: str) -> str:
    return value.replace("\r\n", "\n").strip()


def flatten_release_pages(payload: Any) -> list[Mapping[str, Any]]:
    if not isinstance(payload, list):
        raise ReleaseResolutionError("GitHub Release list response is not a JSON array.")
    releases: list[Mapping[str, Any]] = []
    for page in payload:
        if isinstance(page, list):
            entries = page
        elif isinstance(page, Mapping):
            entries = [page]
        else:
            raise ReleaseResolutionError("GitHub Release list contains an invalid page.")
        if any(not isinstance(entry, Mapping) for entry in entries):
            raise ReleaseResolutionError("GitHub Release list contains an invalid Release.")
        releases.extend(entries)
    return releases


def validate_release(
    release: Any,
    *,
    repository: str,
    tag: str,
    title: str,
    notes: str,
    expected_id: int | None = None,
    require_draft: bool = False,
) -> dict[str, Any]:
    if not isinstance(release, Mapping):
        raise ReleaseResolutionError("GitHub Release response is not a JSON object.")

    release_id = release.get("id")
    if isinstance(release_id, bool) or not isinstance(release_id, int) or release_id <= 0:
        raise ReleaseResolutionError("GitHub Release response has no valid numeric ID.")
    if expected_id is not None and release_id != expected_id:
        raise ReleaseResolutionError("Release ID lookup returned a different Release.")
    expected_api_url = f"{API_ROOT}/{repository}/releases/{release_id}"
    if release.get("url") != expected_api_url:
        raise ReleaseResolutionError("Release belongs to an unexpected repository or has an invalid API URL.")
    if release.get("tag_name") != tag:
        raise ReleaseResolutionError("Release tag does not match the requested version.")
    if release.get("name") != title:
        raise ReleaseResolutionError("Release title does not match the approved request.")
    body = release.get("body")
    if body is None:
        body = ""
    if not isinstance(body, str) or normalize_notes(body) != normalize_notes(notes):
        raise ReleaseResolutionError("Release notes do not match the approved request.")
    if not isinstance(release.get("draft"), bool):
        raise ReleaseResolutionError("Release draft state is missing or invalid.")
    if require_draft and release["draft"] is not True:
        raise ReleaseResolutionError("New Release response is not a Draft.")
    if release.get("target_commitish") != "main":
        raise ReleaseResolutionError("Release target is not the deployment repository main branch.")
    if not isinstance(release.get("assets"), list):
        raise ReleaseResolutionError("Release assets metadata is missing or invalid.")
    html_url = release.get("html_url")
    if html_url is not None and not isinstance(html_url, str):
        raise ReleaseResolutionError("Release browser URL has an invalid format.")
    # Draft URLs may temporarily use an untagged-* slug; the canonical tag and ID
    # from the REST object are authoritative until the Release is published.
    return dict(release)


def find_release_by_tag(
    payload: Any,
    *,
    repository: str,
    tag: str,
    title: str,
    notes: str,
) -> dict[str, Any] | None:
    releases = flatten_release_pages(payload)
    matches = [release for release in releases if release.get("tag_name") == tag]
    if len(matches) > 1:
        raise ReleaseResolutionError("Multiple Releases have the requested tag; refusing modification.")
    if not matches:
        same_draft_title = [
            release
            for release in releases
            if release.get("draft") is True and release.get("name") == title
        ]
        if same_draft_title:
            raise ReleaseResolutionError(
                "A Draft with the requested title exists but its tag or notes differ; refusing to create a duplicate."
            )
        return None
    return validate_release(
        matches[0], repository=repository, tag=tag, title=title, notes=notes
    )


def resolve_release(
    api_call: ApiCall,
    *,
    repository: str,
    tag: str,
    title: str,
    notes: str,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    list_endpoint = f"repos/{repository}/releases?per_page=100"
    # A failed or malformed listing is an error, never evidence that no Release exists.
    listed = api_call("LIST", list_endpoint, None)
    release = find_release_by_tag(
        listed, repository=repository, tag=tag, title=title, notes=notes
    )

    if release is None:
        create_endpoint = f"repos/{repository}/releases"
        create_payload = {
            "tag_name": tag,
            "target_commitish": "main",
            "name": title,
            "body": notes,
            "draft": True,
            "prerelease": False,
        }
        # POST exactly once. If it fails or has a malformed response, fail closed;
        # do not interpret the error as a missing Release and try another creation.
        created = api_call("POST", create_endpoint, create_payload)
        release = validate_release(
            created,
            repository=repository,
            tag=tag,
            title=title,
            notes=notes,
            require_draft=True,
        )

    release_id = release["id"]
    endpoint = f"repos/{repository}/releases/{release_id}"
    for attempt in range(MAX_RELEASE_GET_ATTEMPTS):
        try:
            canonical = api_call("GET", endpoint, None)
        except GitHubAPIError as exc:
            is_retryable = exc.status_code in TRANSIENT_HTTP_STATUSES
            if not is_retryable or attempt + 1 == MAX_RELEASE_GET_ATTEMPTS:
                raise
            sleep(1.0 * (attempt + 1))
            continue
        return validate_release(
            canonical,
            repository=repository,
            tag=tag,
            title=title,
            notes=notes,
            expected_id=release_id,
        )
    raise ReleaseResolutionError("Release ID lookup did not return a verified Release.")


def validate_git_tag_ref(payload: Any, tag: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or payload.get("ref") != f"refs/tags/{tag}":
        raise ReleaseResolutionError("Published Git tag reference is missing or does not match.")
    target = payload.get("object")
    if not isinstance(target, Mapping):
        raise ReleaseResolutionError("Published Git tag target metadata is invalid.")
    if target.get("type") not in {"commit", "tag"} or not isinstance(target.get("sha"), str):
        raise ReleaseResolutionError("Published Git tag target type or SHA is invalid.")
    if not SHA_RE.fullmatch(target["sha"]):
        raise ReleaseResolutionError("Published Git tag target SHA is invalid.")
    return payload


def _github_api_call(method: str, endpoint: str, payload: Mapping[str, Any] | None) -> Any:
    if method == "LIST":
        command = ["gh", "api", "--paginate", "--slurp", endpoint]
        input_text = None
    elif method == "POST":
        command = ["gh", "api", "--method", "POST", endpoint, "--input", "-"]
        input_text = json.dumps(payload, ensure_ascii=False)
    elif method == "GET":
        command = ["gh", "api", endpoint]
        input_text = None
    else:
        raise ReleaseResolutionError(f"Unsupported GitHub API method: {method}")

    result = subprocess.run(command, input=input_text, capture_output=True, text=True, check=False)
    if result.returncode:
        match = re.search(r"HTTP\s+(\d{3})", result.stderr, re.IGNORECASE)
        status = int(match.group(1)) if match else None
        detail = result.stderr.strip() or "GitHub API request failed."
        raise GitHubAPIError(detail, status)
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ReleaseResolutionError("GitHub API returned invalid JSON.") from exc


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve_parser = subparsers.add_parser("resolve")
    resolve_parser.add_argument("--repository", required=True)
    resolve_parser.add_argument("--tag", required=True)
    resolve_parser.add_argument("--title", required=True)
    resolve_parser.add_argument("--notes", required=True)
    resolve_parser.add_argument("--output", type=Path, required=True)

    tag_parser = subparsers.add_parser("validate-tag-ref")
    tag_parser.add_argument("--path", type=Path, required=True)
    tag_parser.add_argument("--tag", required=True)

    args = parser.parse_args()
    try:
        if args.command == "resolve":
            release = resolve_release(
                _github_api_call,
                repository=args.repository,
                tag=args.tag,
                title=args.title,
                notes=args.notes,
            )
            args.output.write_text(
                json.dumps(release, ensure_ascii=False), encoding="utf-8"
            )
            print(f"Verified Release ID {release['id']} (draft={str(release['draft']).lower()}).")
            return 0

        payload = json.loads(args.path.read_text(encoding="utf-8"))
        validate_git_tag_ref(payload, args.tag)
        print(f"Verified Git tag refs/tags/{args.tag}.")
        return 0
    except (GitHubAPIError, ReleaseResolutionError, OSError, json.JSONDecodeError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
