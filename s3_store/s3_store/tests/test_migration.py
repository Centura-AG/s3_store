import os
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from s3_store.s3_store import migration


class TestMigrationUpload(FrappeTestCase):
    def _make_local_file(self, content=b"data"):
        """Create a file on disk + a stub File row pointing at it.
        Uses copy_from_existing_file flag to skip Frappe's file processing
        (which would rewrite file_url) so the migrator can find it at the local path."""
        from frappe.utils import get_files_path

        base = get_files_path(is_private=0)
        os.makedirs(base, exist_ok=True)
        basename = f"s3store-test-{frappe.generate_hash(length=8)}.txt"
        path = os.path.join(base, basename)
        with open(path, "wb") as fh:
            fh.write(content)
        name = frappe.generate_hash(length=10)
        doc = frappe.get_doc(
            {
                "doctype": "File",
                "name": name,
                "file_name": basename,
                "file_url": f"/files/{basename}",
                "is_private": 0,
                "attached_to_doctype": "User",
                "owner": "Administrator",
            }
        )
        doc.flags.copy_from_existing_file = True
        doc.insert(ignore_permissions=True)
        return path, doc.name, basename

    def _settings(self):
        s = SimpleNamespace(
            enabled=1,
            bucket="b",
            key_prefix="p",
            delete_from_s3=1,
            delete_local_after_push=1,
            region="us-east-1",
            endpoint_url="",
            aws_access_key_id="AKIA",
        )
        s.get_password = lambda field: "secret"  # noqa: ARG005
        return s

    def test_upload_local_file_rewrites_url_and_deletes_local(self):
        path, name, basename = self._make_local_file()
        try:
            row = {
                "name": name,
                "file_url": f"/files/{basename}",
                "file_name": basename,
                "is_private": 0,
                "attached_to_doctype": "User",
            }
            with patch("s3_store.s3_store.s3_client.upload") as up:
                ok, err = migration._upload_local_file(
                    row, self._settings(), delete_local=True
                )
            self.assertTrue(ok, err)
            up.assert_called_once()
            self.assertFalse(os.path.exists(path))
            new_url = frappe.db.get_value("File", name, "file_url")
            self.assertIn("/api/method/s3_store.s3_store.api.serve?key=", new_url)
        finally:
            frappe.db.delete("File", {"name": name})
            if os.path.exists(path):
                os.unlink(path)

    def test_upload_missing_local_file_returns_error(self):
        row = {
            "name": "fake",
            "file_url": "/files/missing-xyz.txt",
            "file_name": "missing-xyz.txt",
            "is_private": 0,
            "attached_to_doctype": "User",
        }
        ok, err = migration._upload_local_file(row, self._settings(), delete_local=True)
        self.assertFalse(ok)
        self.assertIn("missing", err.lower())


class TestPushExistingKey(FrappeTestCase):
    def _settings(self):
        s = SimpleNamespace(
            enabled=1,
            bucket="b",
            key_prefix="p",
            region="us-east-1",
            endpoint_url="",
            aws_access_key_id="AKIA",
        )
        s.get_password = lambda field: "secret"  # noqa: ARG005
        return s

    def test_push_existing_key_uploads_and_deletes_local(self):
        from frappe.utils import get_files_path

        from s3_store.s3_store.backup import local_staging_path

        key = "p/2026/05/19/User/deadbeef_report.pdf"
        base = get_files_path(is_private=0)
        os.makedirs(base, exist_ok=True)
        staged_path = local_staging_path(key, is_private=False)
        with open(staged_path, "wb") as fh:
            fh.write(b"staged-bytes")

        row = {
            "name": "F1",
            "file_url": f"/api/method/s3_store.s3_store.api.serve?key={key}",
            "file_name": "report.pdf",
            "is_private": 0,
        }
        try:
            with patch("s3_store.s3_store.s3_client.upload") as up:
                ok, err = migration._push_existing_key(row, key, self._settings())
            self.assertTrue(ok, err)
            up.assert_called_once()
            self.assertEqual(up.call_args.args[0], key)
            self.assertFalse(os.path.exists(staged_path))
        finally:
            if os.path.exists(staged_path):
                os.unlink(staged_path)

    def test_push_existing_key_returns_error_when_local_missing(self):
        key = "p/2026/05/19/User/cafef00d_ghost.pdf"
        row = {
            "name": "F1",
            "file_url": f"/api/method/s3_store.s3_store.api.serve?key={key}",
            "file_name": "ghost.pdf",
            "is_private": 0,
        }
        ok, err = migration._push_existing_key(row, key, self._settings())
        self.assertFalse(ok)
        self.assertIn("missing", err.lower())


class TestPushLocalFilesToS3(FrappeTestCase):
    def test_returns_summary_when_disabled(self):
        with patch.object(migration, "_settings", return_value=None):
            result = migration.push_local_files_to_s3()
        self.assertEqual(result["pushed"], 0)


class TestRun(FrappeTestCase):
    def test_run_marks_failed_when_disabled(self):
        log = frappe.get_doc({"doctype": "S3 Migration Log", "status": "Queued"})
        log.insert(ignore_permissions=True)
        with patch.object(migration, "_settings", return_value=None):
            migration.run(log.name)
        log.reload()
        self.assertEqual(log.status, "Failed")
