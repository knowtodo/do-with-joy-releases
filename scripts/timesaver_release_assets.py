from __future__ import annotations

import argparse
import hashlib
import json
import re
import stat
from pathlib import Path
from typing import Any, Mapping, Sequence

REPAIR_ASSET_NAME = "TIMESAVER_Update_Repair.exe"
SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
POSITIVE_SIZE_RE = re.compile(r"^[1-9][0-9]*$")
VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


class AssetValidationError(ValueError):
    """Candidate or Release asset does not match the approved asset contract."""


def parse_optional_repair_expectations(sha256: Any, size: Any) -> tuple[str, int] | None:
    """Return normalized repair expectations, or None for legacy requests."""
    if sha256 == "" and size == "":
        return None
    if not isinstance(sha256, str) or not isinstance(size, str):
        raise AssetValidationError("Repair asset hash and byte size must be strings.")
    if not SHA256_RE.fullmatch(sha256) or not POSITIVE_SIZE_RE.fullmatch(size):
        raise AssetValidationError("Repair asset requires a valid SHA-256 and positive byte size.")
    return sha256.lower(), int(size)


def expected_candidate_file_names(version: str, include_repair: bool) -> set[str]:
    if not VERSION_RE.fullmatch(version):
        raise AssetValidationError("Candidate version must be X.Y.Z.")
    names = {
        "SHA256SUMS.txt",
        f"TIMESAVER-{version}.zip",
        "VERSION_AND_PATCH_NOTES.txt",
    }
    if include_repair:
        names.add(REPAIR_ASSET_NAME)
    return names


def verify_version_notes(path: Path, version: str) -> str:
    if not VERSION_RE.fullmatch(version):
        raise AssetValidationError("Candidate version must be X.Y.Z.")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise AssetValidationError("Version notes are missing or unreadable.") from exc
    try:
        lines = data.decode("utf-8").splitlines()
    except UnicodeError as exc:
        raise AssetValidationError("Version notes must be UTF-8 text.") from exc
    if (
        not lines
        or lines[0] != f"TIMESAVER v{version} — Release Candidate"
        or f"APP_VERSION: {version}" not in lines
    ):
        raise AssetValidationError("Version notes do not identify the requested candidate version.")
    return hashlib.sha256(data).hexdigest()


def parse_sha256_manifest(path: Path) -> dict[str, str]:
    try:
        lines = path.read_text(encoding="ascii").splitlines()
    except (OSError, UnicodeError) as exc:
        raise AssetValidationError("SHA256SUMS.txt is missing or invalid ASCII.") from exc

    checksums: dict[str, str] = {}
    for line in lines:
        parts = line.split()
        if len(parts) != 2:
            raise AssetValidationError("SHA256SUMS.txt contains a malformed entry.")
        digest, name = parts
        if name.startswith("*"):
            name = name[1:]
        if (
            not SHA256_RE.fullmatch(digest)
            or not name
            or name in {".", ".."}
            or "/" in name
            or "\\" in name
        ):
            raise AssetValidationError("SHA256SUMS.txt contains an invalid hash or filename.")
        if name in checksums:
            raise AssetValidationError("SHA256SUMS.txt contains a duplicate filename.")
        checksums[name] = digest.lower()
    if not checksums:
        raise AssetValidationError("SHA256SUMS.txt is empty.")
    return checksums


def validate_candidate_file_set(root: Path, expected_names: set[str]) -> dict[str, Path]:
    if not root.is_dir() or root.is_symlink():
        raise AssetValidationError("Candidate artifact directory is missing or unsafe.")
    entries = list(root.rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise AssetValidationError("Candidate artifact contains a symbolic link.")
    files = {
        path.relative_to(root).as_posix(): path
        for path in entries
        if path.is_file()
    }
    if set(files) != expected_names:
        raise AssetValidationError(
            f"Unexpected candidate artifact files: {sorted(set(files))}"
        )
    return files


def verify_file(path: Path, expected_sha256: str, expected_size: int, label: str) -> str:
    if not isinstance(expected_sha256, str) or not SHA256_RE.fullmatch(expected_sha256):
        raise AssetValidationError(f"{label} expected SHA-256 is invalid.")
    if isinstance(expected_size, bool) or not isinstance(expected_size, int) or expected_size <= 0:
        raise AssetValidationError(f"{label} expected byte size is invalid.")
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise AssetValidationError(f"{label} is missing or unreadable.") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise AssetValidationError(f"{label} is not a regular file.")
    if metadata.st_size != expected_size:
        raise AssetValidationError(f"{label} byte size mismatch.")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AssetValidationError(f"{label} is unreadable.") from exc
    actual = digest.hexdigest()
    if actual != expected_sha256.lower():
        raise AssetValidationError(f"{label} SHA-256 mismatch.")
    return actual


def release_asset_upload_plan(
    existing_assets: Sequence[Mapping[str, Any]],
    expected_sizes: Mapping[str, int],
    *,
    reject_unexpected: bool = False,
    require_all: bool = False,
) -> list[str]:
    """List only missing approved assets; never overwrite an existing asset."""
    if not expected_sizes:
        raise AssetValidationError("At least one expected Release asset is required.")
    for name, size in expected_sizes.items():
        if not isinstance(name, str) or not name or isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise AssetValidationError("Expected Release asset names and sizes are invalid.")

    counts: dict[str, int] = {}
    by_name: dict[str, Mapping[str, Any]] = {}
    for asset in existing_assets:
        if not isinstance(asset, Mapping):
            raise AssetValidationError("Release asset metadata is invalid.")
        name = asset.get("name")
        if not isinstance(name, str) or not name:
            raise AssetValidationError("Release contains an unnamed asset.")
        counts[name] = counts.get(name, 0) + 1
        if counts[name] > 1 and (reject_unexpected or name in expected_sizes):
            raise AssetValidationError(f"Release contains duplicate asset {name}; refusing overwrite.")
        by_name[name] = asset

    unexpected = set(counts) - set(expected_sizes)
    if reject_unexpected and unexpected:
        raise AssetValidationError(f"Release contains unapproved assets: {sorted(unexpected)}")

    missing: list[str] = []
    for name, expected_size in expected_sizes.items():
        asset = by_name.get(name)
        if asset is None:
            missing.append(name)
            continue
        size = asset.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size != expected_size:
            raise AssetValidationError(f"Existing Release asset {name} has an unexpected byte size.")
    if require_all and missing:
        raise AssetValidationError(f"Release is missing required assets: {missing}")
    return missing


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("path", type=Path)
    verify_parser.add_argument("sha256")
    verify_parser.add_argument("size", type=int)
    verify_parser.add_argument("--label", default="Release asset")

    plan_parser = subparsers.add_parser("plan")
    plan_parser.add_argument("--release-json", type=Path, required=True)
    plan_parser.add_argument("--zip-name", required=True)
    plan_parser.add_argument("--zip-bytes", type=int, required=True)
    plan_parser.add_argument("--repair-name", default="")
    plan_parser.add_argument("--repair-bytes", default="")
    plan_parser.add_argument("--strict", action="store_true")
    plan_parser.add_argument("--require-all", action="store_true")

    args = parser.parse_args()
    try:
        if args.command == "verify":
            digest = verify_file(args.path, args.sha256, args.size, args.label)
            print(f"{args.label} verified: bytes={args.size}, sha256={digest}")
            return 0

        if bool(args.repair_name) != bool(args.repair_bytes):
            raise AssetValidationError("Repair asset name and byte size must be provided together.")
        expected_sizes = {args.zip_name: args.zip_bytes}
        if args.repair_name:
            if args.repair_name != REPAIR_ASSET_NAME:
                raise AssetValidationError("Repair asset name is not approved.")
            if not POSITIVE_SIZE_RE.fullmatch(args.repair_bytes):
                raise AssetValidationError("Repair asset byte size is invalid.")
            expected_sizes[args.repair_name] = int(args.repair_bytes)
        release = json.loads(args.release_json.read_text(encoding="utf-8"))
        missing = release_asset_upload_plan(
            release.get("assets", []),
            expected_sizes,
            reject_unexpected=args.strict,
            require_all=args.require_all,
        )
        for name in missing:
            print(name)
        return 0
    except (AssetValidationError, OSError, json.JSONDecodeError) as exc:
        parser.exit(1, f"::error::{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
