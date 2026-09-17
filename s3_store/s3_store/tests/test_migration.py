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


def _enabled_settings(**over):
    defaults = dict(
        enabled=1,
        bucket="b",
        key_prefix="p",
        delete_local_after_push=1,
        auto_push_after_migrate=1,
        region="us-east-1",
        endpoint_url="",
        aws_access_key_id="AKIA",
    )
    defaults.update(over)
    s = SimpleNamespace(**defaults)
    s.get_password = lambda field: "secret"  # noqa: ARG005
    return s


class TestSettings(FrappeTestCase):
    def test_returns_none_when_the_single_is_unreadable(self):
        with patch.object(frappe, "get_doc", side_effect=RuntimeError("no table")):
            self.assertIsNone(migration._settings())

    def test_returns_none_when_disabled(self):
        with patch.object(frappe, "get_doc", return_value=_enabled_settings(enabled=0)):
            self.assertIsNone(migration._settings())

    def test_returns_the_doc_when_enabled(self):
        settings = _enabled_settings()
        with patch.object(frappe, "get_doc", return_value=settings):
            self.assertIs(migration._settings(), settings)


class TestLocalPathFor(FrappeTestCase):
    def test_resolves_a_plain_file_url(self):
        path = migration._local_path_for("/files/report.pdf", False)
        self.assertTrue(path.endswith("/report.pdf"))

    def test_rejects_a_path_that_escapes_the_files_directory(self):
        with self.assertRaises(frappe.PermissionError):
            migration._local_path_for("/files/..", False)


class TestIterLocalFiles(FrappeTestCase):
    def test_asks_for_both_local_url_shapes(self):
        rows = [{"name": "F1"}]
        with patch.object(frappe, "get_all", return_value=rows) as get_all:
            self.assertEqual(migration._iter_local_files(), rows)
        or_filters = get_all.call_args.kwargs["or_filters"]
        self.assertEqual([f[2] for f in or_filters], ["/files/%", "/private/files/%"])


class TestUploadLocalFileCleanup(FrappeTestCase):
    def test_missing_local_copy_at_delete_time_is_not_an_error(self):
        from frappe.utils import get_files_path

        base = get_files_path(is_private=0)
        os.makedirs(base, exist_ok=True)
        basename = f"s3store-vanish-{frappe.generate_hash(length=8)}.txt"
        path = os.path.join(base, basename)
        with open(path, "wb") as fh:
            fh.write(b"data")

        row = {
            "name": "F-VANISH",
            "file_url": f"/files/{basename}",
            "file_name": basename,
            "is_private": 0,
            "attached_to_doctype": "User",
        }

        def upload(key, fh, content_type, is_private, settings):
            os.unlink(path)

        with (
            patch("s3_store.s3_store.s3_client.upload", side_effect=upload),
            patch.object(frappe.db, "set_value"),
        ):
            ok, err = migration._upload_local_file(
                row, _enabled_settings(), delete_local=True
            )
        self.assertTrue(ok, err)
        self.assertFalse(os.path.exists(path))


class TestRunMigration(FrappeTestCase):
    def _log(self):
        log = frappe.get_doc({"doctype": "S3 Migration Log", "status": "Queued"})
        log.insert(ignore_permissions=True)
        return log

    def _run(self, rows, upload_results):
        log = self._log()
        with (
            patch.object(migration, "_settings", return_value=_enabled_settings()),
            patch.object(migration, "_iter_local_files", return_value=rows),
            patch.object(migration, "_upload_local_file", side_effect=upload_results),
            patch.object(frappe, "publish_progress"),
        ):
            migration.run(log.name)
        log.reload()
        return log

    def test_completed_when_every_file_uploads(self):
        rows = [{"name": f"F{i}"} for i in range(3)]
        log = self._run(rows, [(True, None)] * 3)
        self.assertEqual(log.status, "Completed")
        self.assertEqual(log.total_files, 3)
        self.assertEqual(log.migrated, 3)
        self.assertEqual(log.failed, 0)
        self.assertIsNone(log.errors)
        self.assertIsNotNone(log.completed_at)

    def test_completed_with_errors_when_some_files_fail(self):
        rows = [{"name": "F1"}, {"name": "F2"}]
        log = self._run(rows, [(True, None), (False, "F2: boom")])
        self.assertEqual(log.status, "Completed with errors")
        self.assertEqual(log.migrated, 1)
        self.assertEqual(log.failed, 1)
        self.assertIn("F2: boom", log.errors)

    def test_failed_when_no_file_uploads(self):
        log = self._run([{"name": "F1"}], [(False, "F1: boom")])
        self.assertEqual(log.status, "Failed")
        self.assertEqual(log.migrated, 0)

    def test_progress_is_written_every_fifty_files(self):
        rows = [{"name": f"F{i}"} for i in range(50)]
        log = self._run(rows, [(True, None)] * 50)
        self.assertEqual(log.status, "Completed")
        self.assertEqual(log.migrated, 50)

    def test_error_list_is_capped(self):
        rows = [{"name": f"F{i}"} for i in range(520)]
        log = self._run(rows, [(False, f"F{i}: boom") for i in range(520)])
        self.assertEqual(log.status, "Failed")
        self.assertIn("... and 20 more", log.errors)
        self.assertEqual(len(log.errors.splitlines()), 501)


class TestIterStagedS3Records(FrappeTestCase):
    def test_yields_only_records_whose_bytes_are_on_disk(self):
        from frappe.utils import get_files_path

        base = get_files_path(is_private=0)
        os.makedirs(base, exist_ok=True)
        staged_key = f"p/2026/User/{frappe.generate_hash(length=8)}_here.txt"
        missing_key = f"p/2026/User/{frappe.generate_hash(length=8)}_gone.txt"
        from s3_store.s3_store.backup import local_staging_path

        staged_path = local_staging_path(staged_key, False)
        with open(staged_path, "wb") as fh:
            fh.write(b"bytes")

        rows = [
            ({"name": "F1", "is_private": 0}, staged_key),
            ({"name": "F2", "is_private": 0}, missing_key),
        ]
        try:
            with patch(
                "s3_store.s3_store.backup._iter_s3_file_rows", return_value=rows
            ):
                out = list(migration._iter_staged_s3_records(_enabled_settings()))
            self.assertEqual([key for _, key in out], [staged_key])
        finally:
            os.unlink(staged_path)


class TestPushLocalFilesToS3Flow(FrappeTestCase):
    def test_counts_both_staged_and_local_files(self):
        staged = [({"name": "F1", "is_private": 0}, "p/k1.txt")]
        local = [{"name": "F2"}, {"name": "F3"}]
        with (
            patch.object(migration, "_settings", return_value=_enabled_settings()),
            patch.object(migration, "_iter_staged_s3_records", return_value=staged),
            patch.object(migration, "_push_existing_key", return_value=(True, None)),
            patch.object(migration, "_iter_local_files", return_value=local),
            patch.object(
                migration,
                "_upload_local_file",
                side_effect=[(True, None), (False, "F3: boom")],
            ),
        ):
            result = migration.push_local_files_to_s3(delete_local=False)
        self.assertEqual(result["pushed"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["errors"], ["F3: boom"])

    def test_reports_a_failed_staged_push(self):
        staged = [({"name": "F1", "is_private": 0}, "p/k1.txt")]
        with (
            patch.object(migration, "_settings", return_value=_enabled_settings()),
            patch.object(migration, "_iter_staged_s3_records", return_value=staged),
            patch.object(
                migration, "_push_existing_key", return_value=(False, "F1: missing")
            ),
            patch.object(migration, "_iter_local_files", return_value=[]),
        ):
            result = migration.push_local_files_to_s3()
        self.assertEqual(result["pushed"], 0)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["errors"], ["F1: missing"])


class TestAfterMigratePushLocal(FrappeTestCase):
    def test_does_nothing_when_disabled(self):
        with (
            patch.object(migration, "_settings", return_value=None),
            patch.object(migration, "push_local_files_to_s3") as push,
        ):
            migration.after_migrate_push_local()
        push.assert_not_called()

    def test_does_nothing_when_auto_push_is_off(self):
        settings = _enabled_settings(auto_push_after_migrate=0)
        with (
            patch.object(migration, "_settings", return_value=settings),
            patch.object(migration, "push_local_files_to_s3") as push,
        ):
            migration.after_migrate_push_local()
        push.assert_not_called()

    def test_pushes_and_honours_delete_local(self):
        settings = _enabled_settings(delete_local_after_push=0)
        with (
            patch.object(migration, "_settings", return_value=settings),
            patch.object(
                migration,
                "push_local_files_to_s3",
                return_value={"pushed": 1, "failed": 0, "errors": []},
            ) as push,
        ):
            migration.after_migrate_push_local()
        push.assert_called_once_with(delete_local=False)
