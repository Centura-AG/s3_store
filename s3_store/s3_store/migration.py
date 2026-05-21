"""Bulk migrate local-only File records to S3, plus post-restore push.

Both flows iterate `tabFile` rows whose `file_url` is a local path
(`/files/...` or `/private/files/...`), upload the bytes to S3, rewrite
`file_url` to the S3-served URL, and optionally unlink the local copy."""

from __future__ import annotations

import contextlib
import mimetypes
import os
from urllib.parse import quote

import frappe
from frappe import _
from frappe.utils import get_files_path, now_datetime

from . import s3_client
from .file_handler import SERVE_PATH


def _settings():
    try:
        settings = frappe.get_cached_doc("S3 Store Settings", "S3 Store Settings")
    except Exception:
        return None
    return settings if settings.enabled else None


def _local_path_for(file_url: str, is_private: bool) -> str:
    # /files/foo.png -> sites/<site>/public/files/foo.png
    # /private/files/foo.png -> sites/<site>/private/files/foo.png
    base = get_files_path(is_private=1 if is_private else 0)
    name = file_url.rsplit("/", 1)[-1]
    return os.path.join(base, name)


def _iter_local_files():
    rows = frappe.db.sql(
        """
		SELECT name, file_url, file_name, is_private, attached_to_doctype
		FROM `tabFile`
		WHERE file_url LIKE '/files/%' OR file_url LIKE '/private/files/%'
		""",
        as_dict=True,
    )
    return rows


def _upload_local_file(row, settings, delete_local: bool) -> tuple[bool, str | None]:
    """Upload one local file to S3 and rewrite its file_url. Returns (ok, error)."""
    local_path = _local_path_for(row["file_url"], row["is_private"])

    try:
        key = s3_client.make_key(
            settings.key_prefix or "",
            row.get("attached_to_doctype") or "Misc",
            row["file_name"] or os.path.basename(local_path),
        )
        content_type = (
            mimetypes.guess_type(row["file_name"] or local_path)[0]
            or "application/octet-stream"
        )
        with open(local_path, "rb") as fh:  # nosemgrep: frappe-security-file-traversal
            s3_client.upload(key, fh, content_type, bool(row["is_private"]), settings)

        s3_client.head(key, settings)

        if row["is_private"] or settings.public_file_mode == "Presigned URL":
            new_url = f"{SERVE_PATH}?key={quote(key, safe='')}"
        else:
            new_url = s3_client.public_url(key, settings)

        frappe.db.set_value(
            "File", row["name"], "file_url", new_url, update_modified=False
        )

        if delete_local:
            try:
                os.unlink(local_path)
            except FileNotFoundError:
                pass

        return True, None
    except Exception as e:
        return False, f"{row['name']}: {str(e).splitlines()[0][:200]}"


def run(log_name: str):
    """Bulk migrate all local File records to S3, updating an S3 Migration Log."""
    log = frappe.get_doc("S3 Migration Log", log_name)
    log.status = "Running"
    log.started_at = now_datetime()
    log.save(ignore_permissions=True)
    frappe.db.commit()  # Commit running status before long job starts  # nosemgrep: frappe-manual-commit

    settings = _settings()
    if not settings:
        log.status = "Failed"
        log.errors = "S3 Store is not enabled"
        log.completed_at = now_datetime()
        log.save(ignore_permissions=True)
        frappe.db.commit()  # Commit failure status when settings are disabled  # nosemgrep: frappe-manual-commit
        return

    rows = _iter_local_files()
    log.total_files = len(rows)
    log.save(ignore_permissions=True)
    frappe.db.commit()  # Commit total count before processing files  # nosemgrep: frappe-manual-commit

    errors: list[str] = []
    migrated = 0
    failed = 0

    for i, row in enumerate(rows):
        ok, err = _upload_local_file(
            row, settings, delete_local=bool(settings.delete_local_after_push)
        )
        if ok:
            migrated += 1
        else:
            failed += 1
            if err:
                errors.append(err)

        frappe.publish_progress(
            percent=(i + 1) * 100 / max(len(rows), 1),
            title=_("S3 Migration"),
            description=f"{i + 1}/{len(rows)}",
        )

        # Periodic commit so progress is visible even on long runs
        if (i + 1) % 50 == 0:
            frappe.db.set_value(
                "S3 Migration Log",
                log_name,
                "migrated",
                migrated,
                update_modified=False,
            )
            frappe.db.set_value(
                "S3 Migration Log", log_name, "failed", failed, update_modified=False
            )
            frappe.db.commit()  # Periodic commit so migration progress is visible  # nosemgrep: frappe-manual-commit

    log = frappe.get_doc("S3 Migration Log", log_name)
    log.migrated = migrated
    log.failed = failed
    capped = errors[:500]
    if len(errors) > 500:
        capped.append(f"... and {len(errors) - 500} more")
    log.errors = "\n".join(capped) if capped else None
    log.completed_at = now_datetime()
    log.status = "Failed" if (failed and migrated == 0) else "Completed"
    log.save(ignore_permissions=True)
    frappe.db.commit()  # Commit final migration results  # nosemgrep: frappe-manual-commit


def _push_existing_key(row, key: str, settings) -> tuple[bool, str | None]:
    """Re-upload a staged local file under the S3 key its File record already points to.

    Used in the post-restore flow: `bench backup --with-files` staged S3 bytes into
    `public/files/{key_basename}`, the tar carried them through restore, and we now
    push them back to S3 under the original key — File records don't need rewriting."""
    from .backup import local_staging_path

    local_path = local_staging_path(key, bool(row["is_private"]))
    if not os.path.exists(local_path):
        return False, f"staged file missing for {row['name']}: {local_path}"
    try:
        content_type = (
            mimetypes.guess_type(row["file_name"] or local_path)[0]
            or "application/octet-stream"
        )
        with open(local_path, "rb") as fh:  # nosemgrep: frappe-security-file-traversal
            s3_client.upload(key, fh, content_type, bool(row["is_private"]), settings)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(local_path)
        return True, None
    except Exception as e:
        return False, f"{row['name']}: {e}"


def _iter_staged_s3_records(settings):
    """File records that already point at S3 AND whose bytes are sitting locally
    (i.e. just unpacked from a backup tar). Skips records whose S3 object is fine."""
    from .backup import _iter_s3_file_rows, local_staging_path

    for row, key in _iter_s3_file_rows(settings):
        if os.path.exists(local_staging_path(key, bool(row["is_private"]))):
            yield row, key


def push_local_files_to_s3(delete_local: bool = True) -> dict:
    """Push any locally-staged files back to S3.

    Handles two cases:

    1. **Post-restore** — File records already have S3 URLs (`?key=...` or a
       Public ACL URL); their bytes were carried in the backup tar and now sit
       at `public/files/{key_basename}`. We re-upload to the existing key and
       leave the `file_url` untouched.
    2. **Bulk migration** — File records have local URLs (`/files/...` or
       `/private/files/...`). We mint a fresh S3 key, upload, and rewrite the
       `file_url` to the serve endpoint.

    Returns a summary dict: {"pushed": int, "failed": int, "errors": [str]}."""
    settings = _settings()
    if not settings:
        return {"pushed": 0, "failed": 0, "errors": ["S3 Store is not enabled"]}

    pushed = 0
    failed = 0
    errors: list[str] = []

    for row, key in _iter_staged_s3_records(settings):
        ok, err = _push_existing_key(row, key, settings)
        if ok:
            pushed += 1
        else:
            failed += 1
            if err:
                errors.append(err)

    for row in _iter_local_files():
        ok, err = _upload_local_file(row, settings, delete_local=delete_local)
        if ok:
            pushed += 1
        else:
            failed += 1
            if err:
                errors.append(err)

    frappe.db.commit()  # Commit rewritten file_url values after bulk push  # nosemgrep: frappe-manual-commit
    return {"pushed": pushed, "failed": failed, "errors": errors}


def after_migrate_push_local():
    """after_migrate hook. Opt-in: only runs if settings.auto_push_after_migrate."""
    settings = _settings()
    if not settings or not settings.auto_push_after_migrate:
        return
    result = push_local_files_to_s3(delete_local=bool(settings.delete_local_after_push))
    if result["pushed"] or result["failed"]:
        print(
            f"[s3_store] after_migrate: pushed {result['pushed']}, failed {result['failed']}"
        )
