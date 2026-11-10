import json
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

ZIP_SHA = "C78E1A8FC2546A40327C701E8E6DDB461DC679A322AD900EAA741405B1808B2D"
EXE_SHA = "FC0E418D431D753A3C920199F61868B63B74003CFBAA44A5A9794D905820F333"
UPDATER_SHA = "CEB28ED1E8DFDE50FD1BD8511DE9D05900D4F3C98339AA2D55CEFBC6F49CD498"


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
        self.assertEqual(request_from_dispatch(valid_values())["version"], "1.1.6")

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
            {"application": "timesaver", **valid_values()},
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


if __name__ == "__main__":
    unittest.main()
