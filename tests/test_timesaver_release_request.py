import hashlib
import json
import tempfile
import unittest
from subprocess import CompletedProcess
from unittest.mock import patch
from pathlib import Path

from scripts.resolve_timesaver_release_request import (
    RequestError,
    ensure_version_allowed,
    parse_push_changes,
    request_from_dispatch,
    request_from_push,
    validate_push_context,
    validate_retry_sequence,
    _original_public_version_for_request,
)
from scripts.timesaver_release_assets import (
    AssetValidationError,
    REPAIR_ASSET_NAME,
    expected_candidate_file_names,
    parse_optional_repair_expectations,
    release_asset_upload_plan,
    validate_candidate_file_set,
    verify_file,
)
from scripts.timesaver_release_draft import (
    GitHubAPIError,
    ReleaseResolutionError,
    find_release_by_tag,
    resolve_release,
    validate_git_tag_ref,
)

ZIP_SHA = "C78E1A8FC2546A40327C701E8E6DDB461DC679A322AD900EAA741405B1808B2D"
EXE_SHA = "FC0E418D431D753A3C920199F61868B63B74003CFBAA44A5A9794D905820F333"
UPDATER_SHA = "CEB28ED1E8DFDE50FD1BD8511DE9D05900D4F3C98339AA2D55CEFBC6F49CD498"
REPAIR_SHA = "1FE157DE76AFF90D7F26B24E740E5FF52A0FA13AC1D4C6A5E4C452EDC73DD98C"
REPAIR_BYTES = "12033031"


def valid_values():
    return {
        "version": "1.1.6",
        "source_repository": "knowtodo/timesaver",
        "source_run_id": "42489954978",
        "artifact_name": "TIMESAVER-1.1.6-RELEASE-CANDIDATE",
        "expected_source_commit": "1fab21d2af15b3b5dd8f247ff0376cf59f3fef96",
        "expected_zip_sha256": ZIP_SHA,
        "expected_exe_sha256": EXE_SHA,
        "expected_updater_sha256": UPDATER_SHA,
        "release_notes": "Release notes",
    }


def public_json(request):
    return {
        "app_id": "timesaver",
        "version": request["version"],
        "download_url": (
            "https://github.com/knowtodo/do-with-joy-releases/releases/download/"
            f"timesaver-v{request['version']}/TIMESAVER-{request['version']}.zip"
        ),
        "sha256": request["expected_zip_sha256"].lower(),
        "release_notes": request["release_notes"],
    }


class ReleaseRequestTests(unittest.TestCase):
    def test_valid_dispatch_inputs_resolve(self):
        resolved = request_from_dispatch(valid_values())
        self.assertEqual(resolved["version"], "1.1.6")
        self.assertEqual(resolved["expected_repair_sha256"], "")
        self.assertEqual(resolved["expected_repair_bytes"], "")

    def test_optional_repair_request_schema_is_validated_as_a_pair(self):
        values = valid_values()
        values.update({
            "version": "1.1.8",
            "source_run_id": "42792550252",
            "artifact_name": "TIMESAVER-1.1.8-RELEASE-CANDIDATE",
            "expected_repair_sha256": REPAIR_SHA,
            "expected_repair_bytes": REPAIR_BYTES,
        })
        request = request_from_dispatch(values)
        self.assertEqual(request["expected_repair_sha256"], REPAIR_SHA.lower())
        self.assertEqual(request["expected_repair_bytes"], REPAIR_BYTES)

        for field, value in (
            ("expected_repair_sha256", ""),
            ("expected_repair_bytes", "0"),
            ("expected_repair_sha256", "not-a-hash"),
        ):
            invalid = dict(values)
            invalid[field] = value
            with self.assertRaises(RequestError):
                request_from_dispatch(invalid)

    def test_repair_retry_must_match_original_hash_and_size(self):
        values = valid_values()
        values.update({
            "version": "1.1.8",
            "source_run_id": "42792550252",
            "artifact_name": "TIMESAVER-1.1.8-RELEASE-CANDIDATE",
            "expected_repair_sha256": REPAIR_SHA,
            "expected_repair_bytes": REPAIR_BYTES,
        })
        original = json.dumps({"application": "timesaver", **values})
        self.assertEqual(
            request_from_push(
                "release-requests/timesaver/retries/1.1.8-1.json",
                original,
                original_request_json=original,
                prior_retry_paths=[],
            ),
            request_from_dispatch(values),
        )
        changed = dict(values)
        changed["expected_repair_sha256"] = "0" * 64
        with self.assertRaises(RequestError):
            request_from_push(
                "release-requests/timesaver/retries/1.1.8-1.json",
                json.dumps({"application": "timesaver", **changed}),
                original_request_json=original,
                prior_retry_paths=[],
            )

    def test_new_request_file_resolves(self):
        request = request_from_dispatch(valid_values())
        raw = json.dumps({"application": "timesaver", **request})
        parsed = request_from_push("release-requests/timesaver/1.1.6.json", raw)
        self.assertEqual(parsed, request)

    def test_failed_release_retry_matches_original_request_exactly(self):
        raw = json.dumps({"application": "timesaver", **valid_values()})
        self.assertEqual(
            request_from_push(
                "release-requests/timesaver/retries/1.1.6-1.json",
                raw,
                original_request_json=raw,
                prior_retry_paths=[],
            ),
            request_from_dispatch(valid_values()),
        )

    def test_retry_requires_the_original_request_file(self):
        raw = json.dumps({"application": "timesaver", **valid_values()})
        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            with self.assertRaises(RequestError):
                request_from_push(
                    "release-requests/timesaver/retries/1.1.6-1.json",
                    raw,
                    prior_retry_paths=[],
                )

    def test_retry_rejects_changed_source_fields_or_hashes(self):
        original = json.dumps({"application": "timesaver", **valid_values()})
        changed = valid_values()
        changed["expected_zip_sha256"] = "0" * 64
        with self.assertRaises(RequestError):
            request_from_push(
                "release-requests/timesaver/retries/1.1.6-1.json",
                json.dumps({"application": "timesaver", **changed}),
                original_request_json=original,
                prior_retry_paths=[],
            )

    def test_retry_rejects_other_version_and_unapproved_fields(self):
        raw = json.dumps({"application": "timesaver", **valid_values()})
        with self.assertRaises(RequestError):
            request_from_push(
                "release-requests/timesaver/retries/1.1.7-1.json",
                raw,
                original_request_json=raw,
                prior_retry_paths=[],
            )
        with self.assertRaises(RequestError):
            request_from_push(
                "release-requests/timesaver/retries/1.1.6-1.json",
                json.dumps({"application": "timesaver", **valid_values(), "token": "blocked"}),
                original_request_json=raw,
                prior_retry_paths=[],
            )

    def test_retry_numbers_must_be_unique_and_sequential(self):
        first = "release-requests/timesaver/retries/1.1.6-1.json"
        second = "release-requests/timesaver/retries/1.1.6-2.json"
        with self.assertRaises(RequestError):
            validate_retry_sequence(first, [first])
        with self.assertRaises(RequestError):
            validate_retry_sequence("release-requests/timesaver/retries/1.1.6-3.json", [])
        validate_retry_sequence(second, [first])

    @patch("scripts.resolve_timesaver_release_request.subprocess.run")
    def test_retry_baseline_is_read_from_original_request_parent(self, run):
        run.side_effect = [
            CompletedProcess([], 0, stdout="a" * 40 + "\n", stderr=""),
            CompletedProcess([], 0, stdout="b" * 40 + "\n", stderr=""),
            CompletedProcess(
                [],
                0,
                stdout=json.dumps({"app_id": "timesaver", "version": "1.1.5"}),
                stderr="",
            ),
        ]
        self.assertEqual(_original_public_version_for_request("1.1.6"), "1.1.5")
        self.assertEqual(run.call_count, 3)

    def test_retry_requires_the_public_version_recorded_before_original_request(self):
        request = request_from_dispatch(valid_values())
        prior = {**request, "version": "1.1.5"}
        self.assertEqual(
            ensure_version_allowed(
                public_json(prior), request, run_attempt="1", retry_baseline_version="1.1.5"
            ),
            "new-version",
        )
        with self.assertRaises(RequestError):
            ensure_version_allowed(
                public_json({**request, "version": "1.1.4"}),
                request,
                run_attempt="1",
                retry_baseline_version="1.1.5",
            )
        with self.assertRaises(RequestError):
            ensure_version_allowed(
                public_json(request),
                request,
                run_attempt="1",
                retry_baseline_version="1.1.5",
            )

    def test_invalid_or_mismatched_version_is_rejected(self):
        values = valid_values()
        values["version"] = "01.1.6"
        with self.assertRaises(RequestError):
            request_from_dispatch(values)

        with self.assertRaises(RequestError):
            request_from_push(
                "release-requests/timesaver/1.1.7.json",
                json.dumps({"application": "timesaver", **valid_values()}),
            )

    def test_wrong_artifact_name_or_hash_is_rejected(self):
        values = valid_values()
        values["artifact_name"] = "TIMESAVER-1.1.5-RELEASE-CANDIDATE"
        with self.assertRaises(RequestError):
            request_from_dispatch(values)

        values = valid_values()
        values["expected_zip_sha256"] = "not-a-hash"
        with self.assertRaises(RequestError):
            request_from_dispatch(values)

    def test_unexpected_fields_and_duplicate_json_keys_are_rejected(self):
        data = {"application": "timesaver", **valid_values(), "token": "not-accepted"}
        with self.assertRaises(RequestError):
            request_from_push("release-requests/timesaver/1.1.6.json", json.dumps(data))

        duplicate = '{"application":"timesaver","application":"timesaver"}'
        with self.assertRaises(RequestError):
            request_from_push("release-requests/timesaver/1.1.6.json", duplicate)

    def test_only_one_new_request_file_is_accepted(self):
        tab = chr(9)
        newline = chr(10)
        self.assertEqual(
            parse_push_changes(f"A{tab}release-requests/timesaver/1.1.6.json{newline}"),
            "release-requests/timesaver/1.1.6.json",
        )
        with self.assertRaises(RequestError):
            parse_push_changes(f"M{tab}release-requests/timesaver/1.1.6.json{newline}")
        with self.assertRaises(RequestError):
            parse_push_changes(
                f"A{tab}release-requests/timesaver/1.1.6.json{newline}A{tab}README.md{newline}"
            )
        with self.assertRaises(RequestError):
            parse_push_changes(
                f"A{tab}release-requests/timesaver/retries/1.1.6-1.json{newline}"
                f"A{tab}release-requests/timesaver/retries/1.1.6-2.json{newline}"
            )

    def test_only_deployment_repository_main_push_is_accepted(self):
        validate_push_context(
            "knowtodo/do-with-joy-releases",
            "refs/heads/main",
            "1" * 40,
            "2" * 40,
        )
        with self.assertRaises(RequestError):
            validate_push_context("knowtodo/do-with-joy-releases", "refs/heads/feature", "1" * 40, "2" * 40)
        with self.assertRaises(RequestError):
            validate_push_context("knowtodo/do-with-joy-releases", "refs/heads/main", "0" * 40, "2" * 40)

    def test_public_version_must_increase(self):
        request = request_from_dispatch(valid_values())
        prior = {**request, "version": "1.1.5"}
        self.assertEqual(ensure_version_allowed(public_json(prior), request, run_attempt="1"), "new-version")

        newer = {**request, "version": "1.1.7"}
        with self.assertRaises(RequestError):
            ensure_version_allowed(public_json(newer), request, run_attempt="1")

    def test_already_public_version_requires_exact_retry_and_is_not_redispatched(self):
        request = request_from_dispatch(valid_values())
        current = public_json(request)
        with self.assertRaises(RequestError):
            ensure_version_allowed(current, request, run_attempt="1")
        with self.assertRaises(RequestError):
            ensure_version_allowed({**current, "sha256": "0" * 64}, request, run_attempt="2")
        self.assertEqual(ensure_version_allowed(current, request, run_attempt="2"), "already-current")


class ReleaseAssetValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_legacy_and_repair_candidate_file_sets_are_versioned_exactly(self):
        legacy_root = self.root / "legacy"
        legacy_root.mkdir()
        legacy_names = expected_candidate_file_names("1.1.7", include_repair=False)
        for name in legacy_names:
            (legacy_root / name).write_bytes(b"candidate")
        self.assertNotIn(REPAIR_ASSET_NAME, legacy_names)
        self.assertEqual(set(validate_candidate_file_set(legacy_root, legacy_names)), legacy_names)

        repair_root = self.root / "repair"
        repair_root.mkdir()
        repair_names = expected_candidate_file_names("1.1.8", include_repair=True)
        for name in repair_names:
            (repair_root / name).write_bytes(b"candidate")
        self.assertIn(REPAIR_ASSET_NAME, repair_names)
        self.assertEqual(set(validate_candidate_file_set(repair_root, repair_names)), repair_names)

    def test_missing_or_unapproved_candidate_files_are_rejected(self):
        expected = expected_candidate_file_names("1.1.8", include_repair=True)
        for name in expected - {REPAIR_ASSET_NAME}:
            (self.root / name).write_bytes(b"candidate")
        with self.assertRaises(AssetValidationError):
            validate_candidate_file_set(self.root, expected)
        (self.root / REPAIR_ASSET_NAME).write_bytes(b"candidate")
        (self.root / "unauthorized.exe").write_bytes(b"not approved")
        with self.assertRaises(AssetValidationError):
            validate_candidate_file_set(self.root, expected)

    def test_repair_file_hash_and_size_are_checked_against_actual_bytes(self):
        data = b"standalone repair executable fixture"
        path = self.root / REPAIR_ASSET_NAME
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        self.assertEqual(verify_file(path, digest.upper(), len(data), REPAIR_ASSET_NAME), digest)
        with self.assertRaises(AssetValidationError):
            verify_file(path, "0" * 64, len(data), REPAIR_ASSET_NAME)
        with self.assertRaises(AssetValidationError):
            verify_file(path, digest, len(data) + 1, REPAIR_ASSET_NAME)
        with self.assertRaises(AssetValidationError):
            verify_file(self.root / "missing.exe", digest, len(data), REPAIR_ASSET_NAME)

    def test_draft_reuse_and_partial_upload_retries_only_plan_missing_assets(self):
        expected = {"TIMESAVER-1.1.8.zip": 124495365, REPAIR_ASSET_NAME: 12033031}
        zip_only = [{"name": "TIMESAVER-1.1.8.zip", "size": 124495365}]
        repair_only = [{"name": REPAIR_ASSET_NAME, "size": 12033031}]
        complete = [
            {"name": "TIMESAVER-1.1.8.zip", "size": 124495365},
            {"name": REPAIR_ASSET_NAME, "size": 12033031},
        ]
        self.assertEqual(
            release_asset_upload_plan(zip_only, expected, reject_unexpected=True),
            [REPAIR_ASSET_NAME],
        )
        self.assertEqual(
            release_asset_upload_plan(repair_only, expected, reject_unexpected=True),
            ["TIMESAVER-1.1.8.zip"],
        )
        self.assertEqual(
            release_asset_upload_plan(complete, expected, reject_unexpected=True),
            [],
        )

    def test_duplicate_wrong_size_and_unapproved_assets_fail_without_overwrite(self):
        expected = {"TIMESAVER-1.1.8.zip": 124495365, REPAIR_ASSET_NAME: 12033031}
        with self.assertRaises(AssetValidationError):
            release_asset_upload_plan(
                [
                    {"name": "TIMESAVER-1.1.8.zip", "size": 124495365},
                    {"name": "TIMESAVER-1.1.8.zip", "size": 124495365},
                ],
                expected,
                reject_unexpected=True,
            )
        with self.assertRaises(AssetValidationError):
            release_asset_upload_plan(
                [{"name": REPAIR_ASSET_NAME, "size": 1}],
                expected,
                reject_unexpected=True,
            )
        with self.assertRaises(AssetValidationError):
            release_asset_upload_plan(
                [{"name": "unexpected.exe", "size": 1}],
                expected,
                reject_unexpected=True,
            )
        self.assertEqual(
            release_asset_upload_plan(
                [{"name": "legacy-extra.txt", "size": 10}],
                {"TIMESAVER-1.1.7.zip": 100},
            ),
            ["TIMESAVER-1.1.7.zip"],
        )

    def test_optional_repair_expectations_accept_only_both_empty_or_both_valid(self):
        self.assertIsNone(parse_optional_repair_expectations("", ""))
        self.assertEqual(
            parse_optional_repair_expectations(REPAIR_SHA, REPAIR_BYTES),
            (REPAIR_SHA.lower(), int(REPAIR_BYTES)),
        )
        with self.assertRaises(AssetValidationError):
            parse_optional_repair_expectations(REPAIR_SHA, "")
        with self.assertRaises(AssetValidationError):
            parse_optional_repair_expectations("", REPAIR_BYTES)


class DraftReleaseResolutionTests(unittest.TestCase):
    repository = "knowtodo/do-with-joy-releases"
    tag = "timesaver-v1.1.8"
    title = "TIMESAVER 1.1.8"
    notes = "Approved release notes"

    def release(self, **overrides):
        data = {
            "url": "https://api.github.com/repos/knowtodo/do-with-joy-releases/releases/440303231",
            "html_url": "https://github.com/knowtodo/do-with-joy-releases/releases/tag/untagged-abc123",
            "id": 440303231,
            "tag_name": self.tag,
            "target_commitish": "main",
            "name": self.title,
            "draft": True,
            "assets": [],
            "body": self.notes,
        }
        data.update(overrides)
        return data

    def resolve(self, api_call, **kwargs):
        return resolve_release(
            api_call,
            repository=self.repository,
            tag=self.tag,
            title=self.title,
            notes=self.notes,
            sleep=kwargs.get("sleep", lambda _seconds: None),
        )

    def test_existing_draft_is_resolved_from_release_list_and_verified_by_id(self):
        existing = self.release()
        calls = []

        def api(method, endpoint, payload):
            calls.append((method, endpoint, payload))
            if method == "LIST":
                return [[existing]]
            if method == "GET":
                self.assertEqual(endpoint, f"repos/{self.repository}/releases/440303231")
                return existing
            self.fail(f"Unexpected API call: {method} {endpoint}")

        resolved = self.resolve(api)
        self.assertEqual(resolved["id"], 440303231)
        self.assertTrue(resolved["draft"])
        self.assertIn("untagged-", resolved["html_url"])
        self.assertEqual([call[0] for call in calls], ["LIST", "GET"])

    def test_tag_ref_absence_does_not_hide_existing_draft_or_trigger_creation(self):
        existing = self.release()
        calls = []

        def api(method, endpoint, payload):
            calls.append((method, endpoint))
            if method == "LIST":
                return [[existing]]
            if method == "GET":
                return existing
            if "git/ref/tags/" in endpoint:
                raise GitHubAPIError("HTTP 404: Not Found", 404)
            self.fail(f"Unexpected API call: {method} {endpoint}")

        resolved = self.resolve(api)
        self.assertEqual(resolved["id"], 440303231)
        self.assertNotIn("POST", [method for method, _ in calls])
        self.assertFalse(any("git/ref/tags/" in endpoint for _, endpoint in calls))

    def test_existing_draft_with_each_partial_asset_state_is_resolved(self):
        expected = {
            "TIMESAVER-1.1.8.zip": 124510111,
            "TIMESAVER_Update_Repair.exe": 12032757,
        }
        states = (
            ([], sorted(expected)),
            ([{"name": "TIMESAVER-1.1.8.zip", "size": 124510111}], ["TIMESAVER_Update_Repair.exe"]),
            ([{"name": "TIMESAVER_Update_Repair.exe", "size": 12032757}], ["TIMESAVER-1.1.8.zip"]),
            (
                [
                    {"name": "TIMESAVER-1.1.8.zip", "size": 124510111},
                    {"name": "TIMESAVER_Update_Repair.exe", "size": 12032757},
                ],
                [],
            ),
        )
        for assets, missing in states:
            with self.subTest(assets=assets):
                selected = find_release_by_tag(
                    [[self.release(assets=assets)]],
                    repository=self.repository,
                    tag=self.tag,
                    title=self.title,
                    notes=self.notes,
                )
                self.assertIsNotNone(selected)
                self.assertEqual(
                    release_asset_upload_plan(
                        selected["assets"], expected, reject_unexpected=True
                    ),
                    missing,
                )

    def test_duplicate_matching_release_tags_are_rejected(self):
        with self.assertRaisesRegex(ReleaseResolutionError, "Multiple Releases"):
            find_release_by_tag(
                [[self.release(), self.release(id=440303232,
                    url="https://api.github.com/repos/knowtodo/do-with-joy-releases/releases/440303232")]],
                repository=self.repository,
                tag=self.tag,
                title=self.title,
                notes=self.notes,
            )

    def test_wrong_release_metadata_is_rejected(self):
        for changed in (
            {"name": "TIMESAVER 1.1.9"},
            {"body": "Different release notes"},
            {"tag_name": "timesaver-v1.1.9"},
            {"target_commitish": "another-branch"},
            {"url": "https://api.github.com/repos/other/releases/440303231"},
            {"draft": "true"},
        ):
            with self.subTest(changed=changed):
                with self.assertRaises(ReleaseResolutionError):
                    find_release_by_tag(
                        [[self.release(**changed)]],
                        repository=self.repository,
                        tag=self.tag,
                        title=self.title,
                        notes=self.notes,
                    )

    def test_failed_or_malformed_release_list_never_means_no_release(self):
        calls = []

        def api(method, endpoint, payload):
            calls.append(method)
            raise GitHubAPIError("HTTP 403: Resource not accessible", 403)

        with self.assertRaises(GitHubAPIError):
            self.resolve(api)
        self.assertEqual(calls, ["LIST"])

        calls.clear()

        def malformed(method, endpoint, payload):
            calls.append(method)
            return {"message": "Not Found"}

        with self.assertRaises(ReleaseResolutionError):
            self.resolve(malformed)
        self.assertEqual(calls, ["LIST"])

    def test_empty_successful_list_creates_once_then_uses_returned_id(self):
        created = self.release(
            id=440303999,
            url="https://api.github.com/repos/knowtodo/do-with-joy-releases/releases/440303999",
            assets=[],
        )
        calls = []
        delays = []

        def api(method, endpoint, payload):
            calls.append((method, endpoint, payload))
            if method == "LIST":
                return [[]]
            if method == "POST":
                self.assertEqual(endpoint, f"repos/{self.repository}/releases")
                self.assertEqual(payload["tag_name"], self.tag)
                self.assertEqual(payload["target_commitish"], "main")
                self.assertIs(payload["draft"], True)
                return created
            if method == "GET":
                self.assertEqual(endpoint, f"repos/{self.repository}/releases/440303999")
                return created
            self.fail(f"Unexpected API call: {method} {endpoint}")

        resolved = self.resolve(api, sleep=delays.append)
        self.assertEqual(resolved["id"], 440303999)
        self.assertEqual([method for method, _, _ in calls].count("POST"), 1)
        self.assertEqual(delays, [])

    def test_create_response_without_release_id_fails_without_second_creation(self):
        created = self.release()
        created.pop("id")
        created.pop("url")
        calls = []

        def api(method, endpoint, payload):
            calls.append(method)
            if method == "LIST":
                return []
            if method == "POST":
                return created
            self.fail(f"Unexpected API call: {method} {endpoint}")

        with self.assertRaisesRegex(ReleaseResolutionError, "numeric ID"):
            self.resolve(api)
        self.assertEqual(calls, ["LIST", "POST"])

    def test_create_response_metadata_mismatch_fails_closed(self):
        created = self.release(id=440303999,
            url="https://api.github.com/repos/knowtodo/do-with-joy-releases/releases/440303999",
            name="Unexpected title")
        calls = []

        def api(method, endpoint, payload):
            calls.append(method)
            if method == "LIST":
                return []
            if method == "POST":
                return created
            self.fail(f"Unexpected API call: {method} {endpoint}")

        with self.assertRaisesRegex(ReleaseResolutionError, "title"):
            self.resolve(api)
        self.assertEqual(calls, ["LIST", "POST"])

    def test_release_id_lookup_retries_transient_404_but_never_recreates(self):
        existing = self.release()
        calls = []
        delays = []

        def api(method, endpoint, payload):
            calls.append(method)
            if method == "LIST":
                return [[existing]]
            if method == "GET" and calls.count("GET") < 3:
                raise GitHubAPIError("HTTP 404: Not Found", 404)
            if method == "GET":
                return existing
            self.fail("An existing Release must not be recreated.")

        resolved = self.resolve(api, sleep=delays.append)
        self.assertEqual(resolved["id"], 440303231)
        self.assertEqual(calls.count("GET"), 3)
        self.assertEqual(calls.count("POST"), 0)
        self.assertEqual(delays, [1.0, 2.0])

    def test_release_id_lookup_does_not_retry_authentication_failure(self):
        existing = self.release()
        calls = []

        def api(method, endpoint, payload):
            calls.append(method)
            if method == "LIST":
                return [[existing]]
            if method == "GET":
                raise GitHubAPIError("HTTP 403: Forbidden", 403)
            self.fail("An existing Release must not be recreated.")

        with self.assertRaises(GitHubAPIError):
            self.resolve(api)
        self.assertEqual(calls, ["LIST", "GET"])

    def test_release_id_lookup_rejects_a_different_release(self):
        existing = self.release()
        wrong = self.release(
            id=440303232,
            url="https://api.github.com/repos/knowtodo/do-with-joy-releases/releases/440303232",
        )

        def api(method, endpoint, payload):
            if method == "LIST":
                return [[existing]]
            if method == "GET":
                return wrong
            self.fail(f"Unexpected API call: {method} {endpoint}")

        with self.assertRaisesRegex(ReleaseResolutionError, "different Release"):
            self.resolve(api)

    def test_draft_with_wrong_tag_or_notes_blocks_duplicate_creation(self):
        wrong_tag = self.release(tag_name="untagged-0ead94fd45de03c5ee60")
        wrong_notes = self.release(body="Different release notes")
        for ambiguous in (wrong_tag, wrong_notes):
            with self.subTest(release=ambiguous):
                with self.assertRaisesRegex(ReleaseResolutionError, "tag or notes differ"):
                    find_release_by_tag(
                        [[ambiguous]],
                        repository=self.repository,
                        tag=self.tag,
                        title=self.title,
                        notes=self.notes,
                    )

    def test_published_tag_ref_is_verified_separately_from_release(self):
        valid = {
            "ref": f"refs/tags/{self.tag}",
            "object": {"sha": "a" * 40, "type": "commit", "url": "https://api.github.com/"},
        }
        self.assertEqual(validate_git_tag_ref(valid, self.tag), valid)
        with self.assertRaises(ReleaseResolutionError):
            validate_git_tag_ref({"ref": valid["ref"], "object": {"sha": "bad", "type": "commit"}}, self.tag)
        with self.assertRaises(ReleaseResolutionError):
            validate_git_tag_ref({"ref": "refs/tags/other", "object": valid["object"]}, self.tag)

    def test_unexpected_release_assets_are_rejected_by_existing_asset_planner(self):
        with self.assertRaises(AssetValidationError):
            release_asset_upload_plan(
                [{"name": "TIMESAVER-1.1.8.zip", "size": 124510111},
                 {"name": "unexpected.exe", "size": 7}],
                {
                    "TIMESAVER-1.1.8.zip": 124510111,
                    "TIMESAVER_Update_Repair.exe": 12032757,
                },
                reject_unexpected=True,
            )


if __name__ == "__main__":
    unittest.main()
