import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_pipeline as pipeline

SOURCE_SHA = "a" * 40
ZIP_SHA = "b" * 64
FIXTURE_UPDATER = b"fixture-common-updater"
FIXTURE_EXE = b"fixture-application-executable"


def make_candidate(root, app="timesaver", version="1.2.0"):
    config = pipeline.APPS[app]
    artifact = root / "candidate"
    artifact.mkdir()
    zip_path = artifact / config["zip"].format(version=version)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(config["exe"], FIXTURE_EXE)
        archive.writestr("Updater.exe", FIXTURE_UPDATER)
    manifest = {
        "app_id": app,
        "version": version,
        "source_commit": SOURCE_SHA,
        "runner_image": "windows-2025 image-test",
        "python": "3.13.17",
        "tcl_tk": "8.6/8.6",
        "dependencies": ["PyInstaller==6.21.0"],
        "test_summary": "Ran 163 tests in 1.2s | OK (skipped=2)",
        "zip_entries": [config["exe"], "Updater.exe"],
        "exe_sha256": hashlib.sha256(FIXTURE_EXE).hexdigest(),
        "updater_sha256": hashlib.sha256(FIXTURE_UPDATER).hexdigest(),
        "zip_sha256": hashlib.sha256(zip_path.read_bytes()).hexdigest(),
    }
    (artifact / "build-verification.json").write_text(json.dumps(manifest), encoding="utf-8")
    return artifact


class SourceRunTests(unittest.TestCase):
    def run_record(self):
        return {
            "id": 9123,
            "event": "workflow_dispatch",
            "status": "completed",
            "conclusion": "success",
            "head_branch": "main",
            "head_repository": {"full_name": "knowtodo/timesaver"},
            "path": ".github/workflows/build-timesaver-windows.yml@refs/heads/main",
            "head_sha": SOURCE_SHA,
        }

    def test_accepts_successful_main_candidate_run(self):
        self.assertEqual(pipeline.validate_run_record(self.run_record(), "timesaver", "9123"), SOURCE_SHA)

    def test_rejects_pull_request_or_failed_run(self):
        record = self.run_record()
        record["event"] = "pull_request"
        with self.assertRaises(pipeline.ValidationError):
            pipeline.validate_run_record(record, "timesaver", "9123")
        record = self.run_record()
        record["conclusion"] = "failure"
        with self.assertRaises(pipeline.ValidationError):
            pipeline.validate_run_record(record, "timesaver", "9123")

    def test_rejects_wrong_repository_or_workflow(self):
        record = self.run_record()
        record["head_repository"]["full_name"] = "other/timesaver"
        with self.assertRaises(pipeline.ValidationError):
            pipeline.validate_run_record(record, "timesaver", "9123")
        record = self.run_record()
        record["path"] = ".github/workflows/other.yml"
        with self.assertRaises(pipeline.ValidationError):
            pipeline.validate_run_record(record, "timesaver", "9123")


class CandidateTests(unittest.TestCase):
    def validate(self, artifact, app="timesaver", version="1.2.0"):
        expected = hashlib.sha256(FIXTURE_UPDATER).hexdigest()
        with patch.object(pipeline, "UPDATER_SHA256", expected):
            return pipeline.validate_candidate(artifact, app, version, SOURCE_SHA)

    def test_accepts_exact_timesaver_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.validate(make_candidate(Path(temporary)))
            self.assertEqual(result["version"], "1.2.0")
            self.assertEqual(result["zip_sha256"], hashlib.sha256(Path(result["zip_path"]).read_bytes()).hexdigest())

    def test_accepts_infofinder_zip_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.validate(make_candidate(Path(temporary), "infofinder"), "infofinder")
            self.assertEqual(Path(result["zip_path"]).name, "InfoFinder-1.2.0.zip")

    def test_rejects_extra_artifact_or_zip_member(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifact = make_candidate(Path(temporary))
            (artifact / "unexpected.txt").write_text("x", encoding="utf-8")
            with self.assertRaises(pipeline.ValidationError):
                self.validate(artifact)
        with tempfile.TemporaryDirectory() as temporary:
            artifact = make_candidate(Path(temporary))
            with zipfile.ZipFile(artifact / "TIMESAVER-1.2.0.zip", "a") as archive:
                archive.writestr("license.dat", b"not allowed")
            with self.assertRaises(pipeline.ValidationError):
                self.validate(artifact)

    def test_rejects_bad_source_manifest_zip_hash_and_test_summary(self):
        for field, value in (
            ("source_commit", "c" * 40),
            ("zip_sha256", "0" * 64),
            ("test_summary", "Ran 163 tests | FAILED (failures=1)"),
            ("zip_entries", ["TIMESAVER.exe", "Updater.exe", "license.dat"]),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                artifact = make_candidate(Path(temporary))
                manifest_path = artifact / "build-verification.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest[field] = value
                manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                with self.assertRaises(pipeline.ValidationError):
                    self.validate(artifact)

    def test_rejects_unapproved_updater_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            artifact = make_candidate(Path(temporary))
            with patch.object(pipeline, "UPDATER_SHA256", "0" * 64):
                with self.assertRaises(pipeline.ValidationError):
                    pipeline.validate_candidate(artifact, "timesaver", "1.2.0", SOURCE_SHA)


class UpdateJsonTests(unittest.TestCase):
    def test_timesaver_updates_only_its_own_release_fields(self):
        current = {
            "app_id": "timesaver",
            "version": "1.1.3",
            "download_url": "old",
            "sha256": "0" * 64,
            "release_notes": "old notes",
            "extension": "preserve",
        }
        result = pipeline.update_metadata_value(current, "timesaver", "1.1.4", ZIP_SHA, "- New fix")
        self.assertEqual(result["version"], "1.1.4")
        self.assertEqual(result["sha256"], ZIP_SHA)
        self.assertEqual(result["release_notes"], "New fix")
        self.assertEqual(result["extension"], "preserve")

    def test_infofinder_keeps_cumulative_sections_and_sets_current_notes(self):
        current = {
            "app_id": "infofinder",
            "version": "1.1.3",
            "release_notes": "[v1.1.2]\n- old item\n[v1.1.3]\n- current item",
            "notes": ["current item"],
        }
        result = pipeline.update_metadata_value(current, "infofinder", "1.1.4", ZIP_SHA, "- First\n- Second")
        self.assertTrue(result["release_notes"].startswith(current["release_notes"]))
        self.assertIn("[v1.1.4]\n- First\n- Second", result["release_notes"])
        self.assertEqual(result["notes"], ["First", "Second"])

    def test_rejects_wrong_app_downgrade_same_version_or_bad_hash(self):
        with self.assertRaises(pipeline.ValidationError):
            pipeline.update_metadata_value({"app_id": "timesaver", "version": "1.1.3"}, "infofinder", "1.1.4", ZIP_SHA, "x")
        with self.assertRaises(pipeline.ValidationError):
            pipeline.update_metadata_value({"app_id": "timesaver", "version": "1.1.4"}, "timesaver", "1.1.4", ZIP_SHA, "x")
        with self.assertRaises(pipeline.ValidationError):
            pipeline.update_metadata_value({"app_id": "timesaver", "version": "1.1.3"}, "timesaver", "1.1.4", "bad", "x")


class WorkflowSafetyTests(unittest.TestCase):
    def test_publisher_is_manual_main_only_and_uses_scoped_tokens(self):
        workflow = (ROOT / ".github" / "workflows" / "publish-approved-release.yml").read_text(encoding="utf-8")
        triggers = workflow.split("\npermissions:", 1)[0]
        self.assertIn("workflow_dispatch:", triggers)
        self.assertNotRegex(triggers, r"(?m)^  (?:push|pull_request):")
        self.assertIn("if: github.ref == 'refs/heads/main'", workflow)
        self.assertIn("environment: production-release", workflow)
        self.assertIn("permission-actions: read", workflow)
        self.assertIn("permission-contents: write", workflow)

    def test_json_update_follows_public_asset_verification(self):
        publisher = (ROOT / "scripts" / "publish_release.ps1").read_text(encoding="utf-8")
        self.assertLess(publisher.index("Public ZIP SHA-256 verified"), publisher.index("update-metadata"))
        self.assertIn("already exists", publisher)
        self.assertIn("--draft", publisher)


if __name__ == "__main__":
    unittest.main()
