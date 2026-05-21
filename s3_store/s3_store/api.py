"""Whitelisted endpoints. `serve` resolves a key to a presigned redirect after
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
    from urllib.parse import quote

    from .file_handler import SERVE_PATH

    expected_url = f"{SERVE_PATH}?key={quote(key, safe='')}"
    file_name = frappe.db.get_value("File", {"file_url": expected_url}, "name")

    if not file_name:
        frappe.throw(_("File not found"), frappe.DoesNotExistError)

    file_doc = frappe.get_doc("File", file_name)
    file_doc.check_permission("read")

    settings = frappe.get_cached_doc("S3 Store Settings", "S3 Store Settings")
    if not settings.enabled:
        frappe.throw(_("S3 Store is not enabled"))

    expiry = max(60, min(int(settings.signed_url_expiry or 3600), 604800))
    url = s3_client.presigned_url(
        key,
        os.path.basename(file_doc.file_name or key),
        expiry,
        settings,
    )
    frappe.local.response["type"] = "redirect"
    frappe.local.response["location"] = url


@frappe.whitelist()
def start_migration():
    frappe.only_for(["System Manager", "Administrator"])
    log = frappe.get_doc({"doctype": "S3 Migration Log", "status": "Queued"})
    log.insert(ignore_permissions=True)
    frappe.db.commit()  # Commit before enqueue so the worker can load the log row  # nosemgrep: frappe-manual-commit

    frappe.enqueue(
        "s3_store.s3_store.migration.run",
        log_name=log.name,
        queue="long",
        timeout=3600,
    )
    return {"log": log.name}
