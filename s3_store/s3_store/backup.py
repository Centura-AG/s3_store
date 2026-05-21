"""Monkey-patch `BackupGenerator` so `bench backup --with-files` stages S3 files
locally during the tar step. Encrypted backups work unchanged — encryption runs
*after* `backup_files()` (see frappe/utils/backups.py:202).

The patch is installed once at app import time from `s3_store/__init__.py`. It is
gated by per-site settings, so sites where `S3 Store Settings` is missing or
disabled get the original behaviour."""

from __future__ import annotations

import contextlib
import os
import shutil
from pathlib import Path

import frappe
from frappe import _
from frappe.utils import get_files_path
from frappe.utils.backups import (
    BackupGenerator,
)  # nosemgrep: frappe-monkey-patching-not-allowed

_PATCHED = False
_ORIGINAL_BACKUP_FILES = None
_ORIGINAL_GET_RECENT = None
_RESTORE_PATCHED = False

LOCK_FILENAME = ".s3_store_backup.lock"


def patch_restore_push() -> None:
    global _RESTORE_PATCHED
    if _RESTORE_PATCHED:
        return
    import frappe.installer as _installer

    _orig = _installer.extract_files

    def _patched(site_name, file_path):
        result = _orig(site_name, file_path)
        _post_extract_push(site_name)
        return result

    _installer.extract_files = _patched  # nosemgrep: frappe-monkey-patching-not-allowed
    _RESTORE_PATCHED = True


def _post_extract_push(site_name: str) -> None:
    import frappe as _frappe

    try:
        _frappe.destroy()
        _frappe.init(site_name)
        _frappe.connect()
        from .migration import _settings, push_local_files_to_s3

        settings = _settings()
        if not (settings and settings.auto_push_after_migrate):
            return
        result = push_local_files_to_s3(
            delete_local=bool(settings.delete_local_after_push)
        )
        if result["pushed"] or result["failed"]:
            print(
                f"[s3_store] post-restore: pushed {result['pushed']}, failed {result['failed']}"
            )
    except Exception as exc:
        print(f"[s3_store] post-restore push skipped: {exc}")
    finally:
        with contextlib.suppress(Exception):
            _frappe.destroy()


def patch_backup_generator() -> None:
    global _PATCHED, _ORIGINAL_BACKUP_FILES, _ORIGINAL_GET_RECENT
    if _PATCHED:
        return
    _ORIGINAL_BACKUP_FILES = BackupGenerator.backup_files
    _ORIGINAL_GET_RECENT = BackupGenerator.get_recent_backup
    BackupGenerator.backup_files = (
        _patched_backup_files  # nosemgrep: frappe-monkey-patching-not-allowed
    )
    BackupGenerator.get_recent_backup = (
        _patched_get_recent_backup  # nosemgrep: frappe-monkey-patching-not-allowed
    )
    _PATCHED = True


def _patched_backup_files(self):
    if not _s3_enabled_for_current_site():
        return _ORIGINAL_BACKUP_FILES(self)
    with _stage_s3_files():
        return _ORIGINAL_BACKUP_FILES(self)


def _patched_get_recent_backup(self, older_than, partial=False):
    """Neutralise the file-tar shortcut: a tar created before recent S3 uploads
    would silently exclude them. DB-tar shortcut is fine."""
    db, public, private, conf = _ORIGINAL_GET_RECENT(self, older_than, partial=partial)
    if _s3_enabled_for_current_site():
        public, private = None, None
    return db, public, private, conf


def _s3_enabled_for_current_site() -> bool:
    """Safe to call during bootstrap, fresh restores, or on sites without the app."""
    try:
        if not getattr(frappe.local, "site", None):
            return False
        settings = frappe.get_cached_doc("S3 Store Settings", "S3 Store Settings")
    except Exception:
        return False
    return bool(settings.enabled and settings.include_in_native_backup)


def _iter_s3_file_rows(settings: object) -> list[tuple[dict, str]]:
    """All File rows whose URL resolves to an S3 key under our bucket.

    Pulls both presigned-serve URLs and any HTTP(S) URLs (which `extract_key_from_url`
    will reject if they don't belong to our bucket). Filtering in Python keeps the
    SQL simple and works for both virtual-host AWS URLs (bucket in subdomain) and
    path-style MinIO URLs (bucket in path)."""
    from .file_handler import extract_key_from_url

    rows = frappe.db.sql(
        """
        SELECT name, file_url, file_name, file_size, is_private
        FROM `tabFile`
        WHERE file_url LIKE %(serve)s
           OR file_url LIKE 'http://%%'
           OR file_url LIKE 'https://%%'
        """,
        {"serve": "%/api/method/s3_store.s3_store.api.serve%"},
        as_dict=True,
    )
    out: list[tuple[dict, str]] = []
    for row in rows:
        key = extract_key_from_url(row["file_url"])
        if key:
            out.append((row, key))
    return out


def local_staging_path(key: str, is_private: bool) -> str:
    """Where a given S3 key is staged on disk during backup and looked up after restore.

    Uses the key's basename, which already has a unique `{token8}_` prefix from
    `make_key`, so two files that share the same user-visible `file_name` cannot
    collide. Both backup staging and post-restore push agree on this layout."""
    base = get_files_path(is_private=1 if is_private else 0)
    return os.path.join(base, key.rsplit("/", 1)[-1])


@contextlib.contextmanager
def _stage_s3_files():
    """Download all S3-hosted files for this site into public/files and private/files,
    yield, then remove only the files we staged. Guarantees cleanup even on tar failure."""
    from . import s3_client

    staged: list[Path] = []
    staged_paths: set[Path] = set()
    lock_path = Path(frappe.get_site_path()) / LOCK_FILENAME
    lock_handle = _acquire_lock(lock_path)
    try:
        settings = frappe.get_cached_doc("S3 Store Settings", "S3 Store Settings")
        _preflight_disk_space(settings)

        for row, key in _iter_s3_file_rows(settings):
            local_path = Path(local_staging_path(key, bool(row["is_private"])))
            if local_path in staged_paths:
                frappe.log_error(
                    f"S3 staging: key basename collision for {key}",
                    "S3 Store backup staging",
                )
                continue
            if local_path.is_symlink():
                frappe.log_error(
                    f"S3 staging: refusing to write to symlink {local_path}",
                    "S3 Store backup staging",
                )
                continue
            if local_path.exists():
                continue  # mixed-mode: pre-existing local copy wins
            local_path.parent.mkdir(parents=True, exist_ok=True)
            try:
                s3_client.download_to_path(key, str(local_path), settings)
                staged.append(local_path)
                staged_paths.add(local_path)
            except Exception as e:
                # Remove any partial file left by a failed download before logging.
                with contextlib.suppress(FileNotFoundError):
                    local_path.unlink()
                # Don't abort the whole backup — log and continue. The missing file
                # just won't be in the tar; the DB still has the File record so the
                # user can investigate.
                frappe.log_error(
                    f"S3 staging failed for {key}: {e}", "S3 Store backup staging"
                )

        yield staged
    finally:
        for p in staged:
            with contextlib.suppress(FileNotFoundError):
                p.unlink()
        _release_lock(lock_handle, lock_path)


def _preflight_disk_space(settings: object) -> None:
    """Abort early if there isn't ~110% of S3 content size free locally."""
    total = sum(row["file_size"] or 0 for row, _ in _iter_s3_file_rows(settings))
    if not total:
        return
    required = int(total * 1.1)
    usage = shutil.disk_usage(frappe.get_site_path())
    if usage.free < required:
        frappe.throw(
            _(
                "Insufficient disk space to stage S3 files for backup: "
                "need {0} bytes, have {1} bytes free under {2}. Free up space or disable "
                "include_in_native_backup in S3 Store Settings."
            ).format(required, usage.free, frappe.get_site_path())
        )


def _acquire_lock(lock_path: Path):
    """Acquire an exclusive file lock; raise if another backup is in progress."""
    import errno
    import fcntl

    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "w")  # nosemgrep: frappe-security-file-traversal
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        fh.close()
        if e.errno in (errno.EAGAIN, errno.EACCES):
            frappe.throw(_("Another s3_store backup is already running for this site."))
        raise
    return fh


def _release_lock(fh, lock_path: Path) -> None:
    if fh is None:
        return
    # Unlink before closing: new openers get a fresh inode so they can't
    # race to acquire the lock on the same file we're about to release.
    with contextlib.suppress(FileNotFoundError):
        lock_path.unlink()
    fh.close()
