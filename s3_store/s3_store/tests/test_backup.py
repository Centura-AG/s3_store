from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from s3_store.s3_store import backup


class TestPatchInstallation(FrappeTestCase):
    def test_patch_is_idempotent(self):
        # It was installed at import. Calling again should be a no-op.
        from frappe.utils.backups import BackupGenerator

        before = BackupGenerator.backup_files
        backup.patch_backup_generator()
        self.assertIs(BackupGenerator.backup_files, before)


class TestPatchedBackupFiles(FrappeTestCase):
    def test_passthrough_when_s3_disabled(self):
        gen = MagicMock()
        with (
            patch.object(backup, "_s3_enabled_for_current_site", return_value=False),
            patch.object(backup, "_ORIGINAL_BACKUP_FILES") as orig,
            patch.object(backup, "_stage_s3_files") as stage,
        ):
            backup._patched_backup_files(gen)
            orig.assert_called_once_with(gen)
            stage.assert_not_called()

    def test_staging_runs_when_enabled(self):
        gen = MagicMock()
        with (
            patch.object(backup, "_s3_enabled_for_current_site", return_value=True),
            patch.object(backup, "_stage_s3_files") as stage,
            patch.object(backup, "_ORIGINAL_BACKUP_FILES", return_value=None),
        ):
            stage.return_value.__enter__ = lambda self: []
            stage.return_value.__exit__ = lambda self, *a: None
            backup._patched_backup_files(gen)
            stage.assert_called_once()


class TestPatchedGetRecentBackup(FrappeTestCase):
    def test_neutralises_file_tars_when_enabled(self):
        gen = MagicMock()
        with (
            patch.object(backup, "_s3_enabled_for_current_site", return_value=True),
            patch.object(
                backup,
                "_ORIGINAL_GET_RECENT",
                return_value=("db", "pub", "priv", "conf"),
            ),
        ):
            db, pub, priv, conf = backup._patched_get_recent_backup(gen, 24)
        self.assertEqual(db, "db")
        self.assertIsNone(pub)
        self.assertIsNone(priv)
        self.assertEqual(conf, "conf")

    def test_preserves_file_tars_when_disabled(self):
        gen = MagicMock()
        with (
            patch.object(backup, "_s3_enabled_for_current_site", return_value=False),
            patch.object(
                backup,
                "_ORIGINAL_GET_RECENT",
                return_value=("db", "pub", "priv", "conf"),
            ),
        ):
            db, pub, priv, conf = backup._patched_get_recent_backup(gen, 24)
        self.assertEqual((db, pub, priv, conf), ("db", "pub", "priv", "conf"))


class TestIterS3FileRows(FrappeTestCase):
    def _settings(self, **over):
        defaults = dict(bucket="mybucket", endpoint_url="")
        defaults.update(over)
        return SimpleNamespace(**defaults)

    def test_picks_up_presigned_serve_urls(self):
        row = {
            "name": "F1",
            "file_url": "/api/method/s3_store.s3_store.api.serve?key=p/2026/k.txt",
            "file_name": "k.txt",
            "file_size": 10,
            "is_private": 0,
        }
        with patch.object(frappe.db, "sql", return_value=[row]):
            out = backup._iter_s3_file_rows(self._settings())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][1], "p/2026/k.txt")

    def test_picks_up_virtual_host_aws_urls(self):
        # AWS virtual-host style: bucket is in the *subdomain*, path is the key.
        # Previous LIKE '%/{bucket}/%' filter missed these.
        row = {
            "name": "F2",
            "file_url": "https://mybucket.s3.us-east-1.amazonaws.com/p/2026/k.txt",
            "file_name": "k.txt",
            "file_size": 10,
            "is_private": 0,
        }
        with (
            patch.object(frappe.db, "sql", return_value=[row]),
            patch(
                "s3_store.s3_store.file_handler._get_settings",
                return_value=self._settings(),
            ),
        ):
            out = backup._iter_s3_file_rows(self._settings())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][1], "p/2026/k.txt")

    def test_skips_unrelated_https_urls(self):
        row = {
            "name": "F3",
            "file_url": "https://other-cdn.example.com/somefile.png",
            "file_name": "somefile.png",
            "file_size": 10,
            "is_private": 0,
        }
        with (
            patch.object(frappe.db, "sql", return_value=[row]),
            patch(
                "s3_store.s3_store.file_handler._get_settings",
                return_value=self._settings(),
            ),
        ):
            out = backup._iter_s3_file_rows(self._settings())
        self.assertEqual(out, [])


class TestLocalStagingPath(FrappeTestCase):
    def test_uses_key_basename_to_avoid_collisions(self):
        # Two keys with the same file_name still produce distinct staging paths
        # because the key's basename carries the unique token8 prefix.
        a = backup.local_staging_path("p/2026/05/19/Doc/aaaaaaaa_report.pdf", False)
        b = backup.local_staging_path("p/2026/05/19/Doc/bbbbbbbb_report.pdf", False)
        self.assertNotEqual(a, b)
        self.assertTrue(a.endswith("aaaaaaaa_report.pdf"))
        self.assertTrue(b.endswith("bbbbbbbb_report.pdf"))


class TestStageS3Files(FrappeTestCase):
    def _settings(self, **over):
        defaults = dict(
            enabled=1,
            include_in_native_backup=1,
            bucket="b",
            key_prefix="p",
            region="us-east-1",
            endpoint_url="",
            aws_access_key_id="AKIA",
        )
        defaults.update(over)
        s = SimpleNamespace(**defaults)
        s.get_password = lambda field: "secret"  # noqa: ARG005
        return s

    def test_stage_cleans_up_on_success(self):
        from frappe.utils import get_files_path

        base = Path(get_files_path(is_private=0))
        base.mkdir(parents=True, exist_ok=True)

        def fake_download(key, path, settings):
            Path(path).write_bytes(b"x")

        row = {
            "name": "F1",
            "file_url": "/api/method/s3_store.s3_store.api.serve?key=some/key.txt",
            "file_name": "key.txt",
            "is_private": 0,
        }
        settings = self._settings()
        with (
            patch.object(frappe, "get_cached_doc", return_value=settings),
            patch.object(frappe.db, "sql", return_value=[row]),
            patch.object(backup, "_preflight_disk_space"),
            patch(
                "s3_store.s3_store.s3_client.download_to_path",
                side_effect=fake_download,
            ),
        ):
            with backup._stage_s3_files() as staged:
                self.assertEqual(len(staged), 1)
                self.assertTrue(staged[0].exists())
            # After exit, staged file should be cleaned up
            self.assertFalse(staged[0].exists())

    def test_stage_cleans_up_on_exception(self):
        from frappe.utils import get_files_path

        base = Path(get_files_path(is_private=0))
        base.mkdir(parents=True, exist_ok=True)

        def fake_download(key, path, settings):
            Path(path).write_bytes(b"x")

        row = {
            "name": "F1",
            "file_url": "/api/method/s3_store.s3_store.api.serve?key=some/key2.txt",
            "file_name": "key2.txt",
            "is_private": 0,
        }
        settings = self._settings()
        staged_paths_seen = []
        with (
            patch.object(frappe, "get_cached_doc", return_value=settings),
            patch.object(frappe.db, "sql", return_value=[row]),
            patch.object(backup, "_preflight_disk_space"),
            patch(
                "s3_store.s3_store.s3_client.download_to_path",
                side_effect=fake_download,
            ),
        ):
            with self.assertRaises(RuntimeError):
                with backup._stage_s3_files() as staged:
                    staged_paths_seen.extend(staged)
                    raise RuntimeError("boom")
        for p in staged_paths_seen:
            self.assertFalse(p.exists(), f"{p} not cleaned up after exception")

    def test_skips_when_local_file_exists(self):
        from frappe.utils import get_files_path

        base = Path(get_files_path(is_private=0))
        base.mkdir(parents=True, exist_ok=True)
        pre_existing = base / "preexisting-keep.txt"
        pre_existing.write_bytes(b"original")
        row = {
            "name": "F1",
            "file_url": "/api/method/s3_store.s3_store.api.serve?key=foo/preexisting-keep.txt",
            "file_name": "preexisting-keep.txt",
            "is_private": 0,
        }
        settings = self._settings()
        try:
            with (
                patch.object(frappe, "get_cached_doc", return_value=settings),
                patch.object(frappe.db, "sql", return_value=[row]),
                patch.object(backup, "_preflight_disk_space"),
                patch("s3_store.s3_store.s3_client.download_to_path") as dl,
            ):
                with backup._stage_s3_files() as staged:
                    self.assertEqual(staged, [])
            dl.assert_not_called()
            # Original local file must still exist with original content
            self.assertEqual(pre_existing.read_bytes(), b"original")
        finally:
            pre_existing.unlink(missing_ok=True)
