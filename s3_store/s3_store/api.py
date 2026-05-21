"""Whitelisted endpoints. `serve` proxies S3 content through the backend after
permission checks; `start_migration` enqueues a bulk local→S3 migration."""

from __future__ import annotations

import os

import frappe
from frappe import _

from . import s3_client


@frappe.whitelist(allow_guest=True)  # nosemgrep: guest-whitelisted-method
def serve(key: str):
    if not key:
        frappe.throw(_("Missing file key"), frappe.PermissionError)

    # Exact-match lookup against the URL we wrote in `write_file`. This avoids
    # LIKE wildcard injection and lets MySQL use an index on file_url.
    from urllib.parse import quote, unquote

    from .file_handler import SERVE_PATH

    raw_key = unquote(key)
    expected_url = f"{SERVE_PATH}?key={quote(raw_key, safe='')}"
    file_name = frappe.db.get_value("File", {"file_url": expected_url}, "name")

    if not file_name:
        frappe.throw(_("File not found"), frappe.DoesNotExistError)

    file_doc = frappe.get_doc("File", file_name)
    file_doc.check_permission("read")

    settings = frappe.get_cached_doc("S3 Store Settings", "S3 Store Settings")
    if not settings.enabled:
        frappe.throw(_("S3 Store is not enabled"))

    obj = s3_client.get_object(raw_key, settings)
    frappe.local.response.update(
        {
            "type": "download",
            "filename": os.path.basename(file_doc.file_name or raw_key),
            "filecontent": obj["Body"].read(),
            "content_type": obj.get("ContentType", "application/octet-stream"),
            "display_content_as": "inline",
        }
    )


@frappe.whitelist()
def start_migration():
    frappe.only_for(["System Manager", "Administrator"])
    log = frappe.get_doc({"doctype": "S3 Migration Log", "status": "Queued"})
    log.insert(ignore_permissions=True)
    frappe.db.commit()  # Commit before enqueue so the worker can load the log row  # nosemgrep: frappe-manual-commit

    frappe.enqueue(
        "s3_store.s3_store.migration.run",
        log_name=log.name,
        queue="default",
        timeout=3600,
    )
    return {"log": log.name}
