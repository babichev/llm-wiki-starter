"""Integration checks use only disposable synthetic vaults, never personal data."""
import hashlib
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

KIT = Path(__file__).resolve().parents[1]
SCRIPT = KIT / "scripts/wiki.py"


class WikiIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="wiki-test-", dir="/private/tmp" if Path("/private/tmp").is_dir() else None)
        self.root = Path(self.tmp.name).resolve()
        self.vault = self.root / "personal-vault"
        self.cli("init")

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, ok=True, vault=None):
        cmd = [sys.executable, str(SCRIPT), "--vault", str(vault or self.vault), *args]
        process = subprocess.run(cmd, text=True, capture_output=True)
        if ok:
            self.assertEqual(process.returncode, 0, process.stderr + process.stdout)
            return json.loads(process.stdout)
        self.assertNotEqual(process.returncode, 0, process.stdout)
        return json.loads(process.stderr or process.stdout)

    def start(self):
        return self.cli("start-run")["run_id"]

    def test_init_idempotent_and_collision_preflight(self):
        result = self.cli("init")
        self.assertEqual(result["copied"], 0)
        home = self.vault / "second-brain-home.md"
        home.write_text("manual edits", encoding="utf-8")
        absent = self.vault / "system/scripts/wiki.py"
        absent.unlink()
        result = self.cli("init", ok=False)
        self.assertIn("overwrite", result["error"])
        self.assertEqual(home.read_text(), "manual edits")
        self.assertFalse(absent.exists(), "Init must fail before adding any missing file")

    def test_init_rejects_kit_nested_and_symlink(self):
        for path in (KIT, KIT / "vault", KIT / "new-vault"):
            self.cli("init", vault=path, ok=False)
        linked = self.root / "linked"
        linked.symlink_to(self.vault, target_is_directory=True)
        self.cli("init", vault=linked, ok=False)

    def test_lease_owned_and_expired_no_takeover(self):
        run = self.start()
        self.cli("start-run", ok=False)
        self.cli("renew-run", "--run", "foreign", ok=False)
        self.cli("finish-run", "--run", "foreign", ok=False)
        lease_path = self.vault / "system/state/writer.lock/lease.json"
        lease = json.loads(lease_path.read_text())
        lease["expires_at"] = "2000-01-01T00:00:00+00:00"
        lease_path.write_text(json.dumps(lease))
        self.cli("start-run", ok=False)
        self.cli("renew-run", "--run", run, ok=False)
        self.cli("finish-run", "--run", run)
        self.assertIsNone(self.cli("status")["lease"])

    def test_immutable_ingest_idempotency_and_mark(self):
        run = self.start()
        common = ["--run", run, "--kind", "local", "--key", "sample-1", "--title", "Synthetic source"]
        first = self.cli("ingest", *common, "--content", "first")
        same = self.cli("ingest", *common, "--content", "first")
        self.assertFalse(same["changed"])
        self.cli("mark", "--run", run, "--key", "sample-1", "--expected-sha256", first["sha256"], "--state", "processed", "--links", "wiki/absent.md", ok=False)
        target = self.vault / "wiki/synthetic.md"
        target.parent.mkdir(exist_ok=True)
        target.write_text("Synthetic reviewed knowledge", encoding="utf-8")
        self.cli("mark", "--run", run, "--key", "sample-1", "--expected-sha256", first["sha256"], "--state", "processed", "--links", "wiki/synthetic.md")
        self.assertEqual(self.cli("ingest", *common, "--content", "first")["state"], "processed")
        second = self.cli("ingest", *common, "--content", "second")
        self.assertEqual(second["state"], "pending")
        self.assertNotEqual(first["snapshot"], second["snapshot"])
        self.assertEqual((self.vault / first["snapshot"]).read_text(), "first")
        self.assertEqual((self.vault / second["snapshot"]).read_text(), "second")
        self.cli("finish-run", "--run", run)

    def test_same_content_metadata_refreshes_without_duplicate_snapshot(self):
        run = self.start()
        target = self.vault / "wiki/synthetic.md"
        target.write_text("Reviewed synthetic knowledge", encoding="utf-8")
        common = ["--run", run, "--kind", "local", "--key", "metadata-source", "--content", "same source"]
        original_meta = ["--title", "Original title", "--coverage", "partial", "--source-updated-at", "2026-01-01", "--evidence-kind", "ai-summary", "--access", "private", "--url", "https://example.org/old"]
        first = self.cli("ingest", *common, *original_meta)
        self.cli("mark", "--run", run, "--key", "metadata-source", "--expected-sha256", first["sha256"], "--state", "processed", "--links", "wiki/synthetic.md")
        self.assertEqual(self.cli("ingest", *common, *original_meta)["state"], "processed")
        updated_meta = ["--title", "Corrected title", "--coverage", "full", "--source-updated-at", "2026-01-02", "--evidence-kind", "primary", "--access", "confidential", "--url", "https://example.org/new"]
        changed = self.cli("ingest", *common, *updated_meta)
        self.assertTrue(changed["metadata_changed"])
        self.assertFalse(changed["content_changed"])
        self.assertEqual(changed["state"], "pending")
        self.assertEqual(changed["snapshot"], first["snapshot"])
        self.assertEqual(len(list((self.vault / "raw").rglob("*.txt"))), 1)
        item = json.loads((self.vault / "system/state/sources.json").read_text())["sources"]["metadata-source"]
        self.assertEqual(item["coverage"], "full")
        self.assertEqual(item["source_updated_at"], "2026-01-02")
        self.assertEqual(item["evidence_kind"], "primary")
        self.assertEqual(item["access"], "confidential")
        self.assertEqual(len(item["versions"]), 2)
        self.assertEqual(item["versions"][0]["coverage"], "partial")
        self.assertEqual(item["versions"][1]["coverage"], "full")
        self.assertFalse(self.cli("ingest", *common, *updated_meta)["changed"])
        self.cli("finish-run", "--run", run)

    def test_ingest_and_mark_refuse_missing_or_corrupt_raw(self):
        run = self.start()
        (self.vault / "wiki/synthetic.md").write_text("Synthetic knowledge", encoding="utf-8")
        for failure in ("missing", "corrupt"):
            common = ["--run", run, "--kind", "local", "--key", failure, "--title", "Synthetic", "--content", "same source"]
            first = self.cli("ingest", *common)
            raw = self.vault / first["snapshot"]
            if failure == "missing":
                raw.unlink()
            else:
                raw.write_text("corrupted", encoding="utf-8")
            ingest_error = self.cli("ingest", *common, ok=False)
            self.assertIn(failure, ingest_error["error"])
            mark_error = self.cli("mark", "--run", run, "--key", failure, "--expected-sha256", first["sha256"], "--state", "processed", "--links", "wiki/synthetic.md", ok=False)
            self.assertIn(failure, mark_error["error"])
        self.cli("finish-run", "--run", run)

    def test_mark_refuses_stale_sha_and_records_reason(self):
        run = self.start()
        (self.vault / "wiki/synthetic.md").write_text("Synthetic knowledge", encoding="utf-8")
        common = ["--run", run, "--kind", "local", "--key", "stale", "--title", "Synthetic"]
        first = self.cli("ingest", *common, "--content", "old source")
        latest = self.cli("ingest", *common, "--content", "new source")
        error = self.cli("mark", "--run", run, "--key", "stale", "--expected-sha256", first["sha256"], "--state", "processed", "--links", "wiki/synthetic.md", ok=False)
        self.assertIn("Stale", error["error"])
        self.cli("mark", "--run", run, "--key", "stale", "--expected-sha256", latest["sha256"], "--state", "needs-review", "--reason", "Source has open questions", "--links", "wiki/synthetic.md")
        item = json.loads((self.vault / "system/state/sources.json").read_text())["sources"]["stale"]
        self.assertEqual(item["mark_reason"], "Source has open questions")
        self.assertEqual(item["marked_sha256"], latest["sha256"])
        self.cli("finish-run", "--run", run)

    def test_index_preserves_manual_and_yaml(self):
        run = self.start()
        index = self.vault / "wiki/index.md"
        before = index.read_text(encoding="utf-8")
        self.assertIn("<!-- GENERATED:START -->", before)
        self.cli("index", "--run", run)
        after = index.read_text(encoding="utf-8")
        for marker, before_part, after_part in [("prefix", before.split("<!-- GENERATED:START -->")[0], after.split("<!-- GENERATED:START -->")[0]), ("suffix", before.split("<!-- GENERATED:END -->")[1], after.split("<!-- GENERATED:END -->")[1])]:
            self.assertEqual(before_part, after_part, marker)
        self.assertFalse(self.cli("index", "--run", run)["changed"])
        self.cli("finish-run", "--run", run)

    def test_backup_restore_hashes_and_tampering(self):
        snapshot = self.root / "backup"
        receipt = self.cli("backup", "--destination", str(snapshot))
        self.cli("verify-backup", "--snapshot", str(snapshot), "--manifest-sha256", receipt["manifest_sha256"])
        restored = self.root / "restored"
        self.cli("restore", "--snapshot", str(snapshot), "--target", str(restored), "--manifest-sha256", receipt["manifest_sha256"])
        self.assertEqual((restored / "second-brain-home.md").read_bytes(), (self.vault / "second-brain-home.md").read_bytes())
        self.cli("restore", "--snapshot", str(snapshot), "--target", str(self.vault), ok=False)
        source = next((snapshot / "files").rglob("*.md"))
        source.write_text("tampered", encoding="utf-8")
        self.cli("verify-backup", "--snapshot", str(snapshot), ok=False)

    def test_backup_symlinks_and_restore_traversal(self):
        (self.vault / "inbox/link.txt").symlink_to(self.vault / "second-brain-home.md")
        snapshot = self.root / "backup"
        receipt = self.cli("backup", "--destination", str(snapshot))
        self.assertIn("inbox/link.txt", receipt["skipped"])
        manifest_path = snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"]["../outside.txt"] = {"sha256": hashlib.sha256(b"unsafe").hexdigest(), "bytes": 6}
        manifest_path.write_text(json.dumps(manifest))
        self.cli("restore", "--snapshot", str(snapshot), "--target", str(self.root / "restore"), ok=False)
        self.assertFalse((self.root / "outside.txt").exists())

    def test_restore_rejects_absolute_paths_and_snapshot_symlinks(self):
        snapshot = self.root / "backup"
        self.cli("backup", "--destination", str(snapshot))
        manifest_path = snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        original = manifest_path.read_bytes()
        manifest["files"][str(self.root / "outside.txt")] = {"sha256": hashlib.sha256(b"unsafe").hexdigest(), "bytes": 6}
        manifest_path.write_text(json.dumps(manifest))
        target = self.root / "restore-absolute"
        self.cli("restore", "--snapshot", str(snapshot), "--target", str(target), ok=False)
        self.assertFalse(target.exists())
        self.assertFalse((self.root / "outside.txt").exists())
        manifest_path.write_bytes(original)
        rel = next(iter(manifest["files"]))
        source = snapshot / "files" / rel
        source.unlink()
        source.symlink_to(self.vault / rel)
        target = self.root / "restore-symlink"
        self.cli("restore", "--snapshot", str(snapshot), "--target", str(target), ok=False)
        self.assertFalse(target.exists())

    def test_backup_refuses_active_foreign_writer(self):
        run = self.start()
        self.cli("backup", "--destination", str(self.root / "without-token"), ok=False)
        self.cli("backup", "--destination", str(self.root / "owned"), "--run", run)
        self.cli("finish-run", "--run", run)

    def test_template_audit_and_duplicate_id_broken_link(self):
        audit = self.cli("audit")
        self.assertTrue(audit["ok"])
        duplicate = self.vault / "wiki/duplicate.md"
        duplicate.write_text((self.vault / "second-brain-home.md").read_text() + "\n[[missing-page]]\n")
        result = self.cli("audit", ok=False)
        self.assertTrue(any("duplicate id" in row["error"] for row in result["errors"]))
        self.assertTrue(any("broken wikilink" in row["error"] for row in result["errors"]))

    def test_installed_script_autodetects_vault(self):
        installed = self.vault / "system/scripts/wiki.py"
        result = subprocess.run([sys.executable, str(installed), "status"], cwd=self.root, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["vault"], str(self.vault))


if __name__ == "__main__":
    unittest.main()
