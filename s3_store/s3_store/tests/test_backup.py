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

    def test_picks_up_serve_urls(self):
        row = {
            "name": "F1",
            "file_url": "/api/method/s3_store.s3_store.api.serve?key=p/2026/k.txt",
            "file_name": "k.txt",
            "file_size": 10,
            "is_private": 0,
        }
        with patch.object(frappe, "get_all", return_value=[row]):
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
            patch.object(frappe, "get_all", return_value=[row]),
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
            patch.object(frappe, "get_all", return_value=[row]),
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
            patch.object(frappe, "get_all", return_value=[row]),
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
            patch.object(frappe, "get_all", return_value=[row]),
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
                patch.object(frappe, "get_all", return_value=[row]),
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


class TestPatchRestorePush(FrappeTestCase):
    def test_wrapper_delegates_and_pushes(self):
        import frappe.installer as installer

        original = installer.extract_files
        seen = []

        def fake_extract(site_name, file_path):
            seen.append((site_name, file_path))
            return "extracted"

        installer.extract_files = fake_extract
        try:
            with (
                patch.object(backup, "_RESTORE_PATCHED", False),
                patch.object(backup, "_post_extract_push") as push,
            ):
                backup.patch_restore_push()
                result = installer.extract_files("site1", "/tmp/files.tar")
            self.assertEqual(result, "extracted")
            self.assertEqual(seen, [("site1", "/tmp/files.tar")])
            push.assert_called_once_with("site1")
        finally:
            installer.extract_files = original

    def test_second_install_is_a_no_op(self):
        import frappe.installer as installer

        before = installer.extract_files
        with patch.object(backup, "_RESTORE_PATCHED", True):
            backup.patch_restore_push()
        self.assertIs(installer.extract_files, before)


class TestPatchBackupGenerator(FrappeTestCase):
    def test_installs_wrappers_and_records_originals(self):
        from frappe.utils.backups import BackupGenerator

        with (
            patch.object(backup, "_PATCHED", False),
            patch.object(backup, "_ORIGINAL_BACKUP_FILES", None),
            patch.object(backup, "_ORIGINAL_GET_RECENT", None),
        ):
            backup.patch_backup_generator()
            self.assertIs(BackupGenerator.backup_files, backup._patched_backup_files)
            self.assertIs(
                BackupGenerator.get_recent_backup, backup._patched_get_recent_backup
            )
            self.assertIsNotNone(backup._ORIGINAL_BACKUP_FILES)
            self.assertIsNotNone(backup._ORIGINAL_GET_RECENT)
            self.assertTrue(backup._PATCHED)


class TestPostExtractPush(FrappeTestCase):
    def _settings(self, **over):
        defaults = dict(auto_push_after_migrate=1, delete_local_after_push=1)
        defaults.update(over)
        return SimpleNamespace(**defaults)

    def _run(self, settings, result=None):
        result = result or {"pushed": 0, "failed": 0, "errors": []}
        with (
            patch.object(frappe, "destroy"),
            patch.object(frappe, "init"),
            patch.object(frappe, "connect"),
            patch("s3_store.s3_store.migration._settings", return_value=settings),
            patch(
                "s3_store.s3_store.migration.push_local_files_to_s3",
                return_value=result,
            ) as push,
        ):
            backup._post_extract_push("site1")
        return push

    def test_pushes_when_auto_push_is_on(self):
        push = self._run(self._settings(), {"pushed": 2, "failed": 1, "errors": ["x"]})
        push.assert_called_once_with(delete_local=True)

    def test_skips_when_auto_push_is_off(self):
        push = self._run(self._settings(auto_push_after_migrate=0))
        push.assert_not_called()

    def test_skips_when_s3_is_disabled(self):
        push = self._run(None)
        push.assert_not_called()

    def test_swallows_errors_so_restore_still_finishes(self):
        with (
            patch.object(frappe, "destroy"),
            patch.object(frappe, "init", side_effect=RuntimeError("no site")),
            patch.object(frappe, "connect"),
        ):
            backup._post_extract_push("site1")


class TestPreflightDiskSpace(FrappeTestCase):
    def _rows(self, *sizes):
        return [({"file_size": size}, f"key{i}") for i, size in enumerate(sizes)]

    def test_no_check_when_nothing_to_stage(self):
        with patch.object(backup.shutil, "disk_usage") as usage:
            backup._preflight_disk_space(self._rows(0, None))
        usage.assert_not_called()

    def test_passes_when_enough_space_is_free(self):
        usage = SimpleNamespace(total=0, used=0, free=10_000)
        with patch.object(backup.shutil, "disk_usage", return_value=usage):
            backup._preflight_disk_space(self._rows(1000, 2000))

    def test_throws_when_free_space_is_below_110_percent(self):
        usage = SimpleNamespace(total=0, used=0, free=1050)
        with (
            patch.object(backup.shutil, "disk_usage", return_value=usage),
            self.assertRaises(frappe.ValidationError),
        ):
            backup._preflight_disk_space(self._rows(1000))


class TestBackupLock(FrappeTestCase):
    def _lock_path(self):
        return (
            Path(frappe.get_site_path()) / f"test-{frappe.generate_hash(length=8)}.lock"
        )

    def test_contention_is_reported_as_a_clear_error(self):
        import errno
        import fcntl

        lock_path = self._lock_path()
        with (
            patch.object(fcntl, "flock", side_effect=OSError(errno.EAGAIN, "busy")),
            self.assertRaises(frappe.ValidationError),
        ):
            backup._acquire_lock(lock_path)
        lock_path.unlink(missing_ok=True)

    def test_unexpected_lock_errors_are_reraised(self):
        import errno
        import fcntl

        lock_path = self._lock_path()
        with (
            patch.object(fcntl, "flock", side_effect=OSError(errno.ENOLCK, "no locks")),
            self.assertRaises(OSError),
        ):
            backup._acquire_lock(lock_path)
        lock_path.unlink(missing_ok=True)

    def test_release_removes_the_lock_file(self):
        lock_path = self._lock_path()
        handle = backup._acquire_lock(lock_path)
        self.assertTrue(lock_path.exists())
        backup._release_lock(handle, lock_path)
        self.assertFalse(lock_path.exists())
        backup._acquire_lock(lock_path).close()
        lock_path.unlink(missing_ok=True)

    def test_release_without_a_handle_is_a_no_op(self):
        backup._release_lock(None, self._lock_path())


class TestStageS3FilesEdgeCases(FrappeTestCase):
    def _settings(self):
        s = SimpleNamespace(
            enabled=1,
            include_in_native_backup=1,
            bucket="b",
            key_prefix="p",
            region="us-east-1",
            endpoint_url="",
            aws_access_key_id="AKIA",
        )
        s.get_password = lambda field: "secret"  # noqa: ARG005
        return s

    def _row(self, key, name="F1"):
        return {
            "name": name,
            "file_url": f"/api/method/s3_store.s3_store.api.serve?key={key}",
            "file_name": key.rsplit("/", 1)[-1],
            "file_size": 1,
            "is_private": 0,
        }

    def _stage(self, rows, download):
        with (
            patch.object(frappe, "get_cached_doc", return_value=self._settings()),
            patch.object(frappe, "get_all", return_value=rows),
            patch.object(backup, "_preflight_disk_space"),
            patch.object(frappe, "log_error") as log,
            patch(
                "s3_store.s3_store.s3_client.download_to_path", side_effect=download
            ) as dl,
        ):
            with backup._stage_s3_files() as staged:
                staged_seen = list(staged)
        return staged_seen, dl, log

    def test_downloads_a_key_basename_only_once(self):
        basename = f"collide-{frappe.generate_hash(length=6)}.txt"
        rows = [
            self._row(f"a/2026/{basename}", "F1"),
            self._row(f"b/2027/{basename}", "F2"),
        ]

        def download(key, path, settings):
            Path(path).write_bytes(b"x")

        staged, dl, log = self._stage(rows, download)
        self.assertEqual(len(staged), 1)
        self.assertEqual(dl.call_count, 1)
        self.assertIn("collision", log.call_args.args[0])

    def test_refuses_to_write_through_a_symlink(self):
        from frappe.utils import get_files_path

        base = Path(get_files_path(is_private=0))
        base.mkdir(parents=True, exist_ok=True)
        target = base / f"symlink-target-{frappe.generate_hash(length=6)}.txt"
        link = base / f"symlink-{frappe.generate_hash(length=6)}.txt"
        link.symlink_to(target)
        try:
            staged, dl, log = self._stage([self._row(f"a/{link.name}")], None)
            self.assertEqual(staged, [])
            dl.assert_not_called()
            self.assertIn("symlink", log.call_args.args[0])
            self.assertFalse(target.exists())
        finally:
            link.unlink(missing_ok=True)
            target.unlink(missing_ok=True)

    def test_a_failed_download_is_logged_and_leaves_no_partial_file(self):
        key = f"a/failed-{frappe.generate_hash(length=6)}.txt"

        def download(key, path, settings):
            Path(path).write_bytes(b"partial")
            raise RuntimeError("connection reset")

        staged, dl, log = self._stage([self._row(key)], download)
        self.assertEqual(staged, [])
        self.assertFalse(Path(backup.local_staging_path(key, False)).exists())
        self.assertIn("S3 staging failed", log.call_args.args[0])
