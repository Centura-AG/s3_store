"""write_file / delete_file_data_content hook implementations.

The `write_file` hook is dispatched by frappe.core.doctype.file.file.File.save_file
(at file.py:712). It receives the File doc; the bytes are in `doc._content`.
We return the same dict shape as `save_file_on_filesystem` so calling code is unaffected.
"""

from __future__ import annotations

import mimetypes
from io import BytesIO
from urllib.parse import quote, urlparse, parse_qs

import frappe
from frappe import _

from . import s3_client
from .doctype.s3_store_settings.s3_store_settings import S3StoreSettings


SERVE_PATH = "/api/method/s3_store.s3_store.api.serve"


def _get_settings() -> S3StoreSettings | None:
    try:
        settings = frappe.get_cached_doc("S3 Store Settings", "S3 Store Settings")
    except frappe.DoesNotExistError:
        return None
    return settings if settings.enabled else None


def _should_ignore(doc, settings: S3StoreSettings) -> bool:
    ignored = settings.get_ignored_doctypes()
    return bool(doc.attached_to_doctype and doc.attached_to_doctype in ignored)


def write_file(doc):
    settings = _get_settings()
    if not settings or _should_ignore(doc, settings):
        return doc.save_file_on_filesystem()

    if isinstance(doc._content, str):
        doc._content = doc._content.encode()

    content_type = (
        doc.content_type
        or (doc.file_name and mimetypes.guess_type(doc.file_name)[0])
        or "application/octet-stream"
    )
    key = s3_client.make_key(
        settings.key_prefix or "", doc.attached_to_doctype or "Misc", doc.file_name
    )
    is_private = bool(doc.is_private)

    s3_client.upload(key, BytesIO(doc._content), content_type, is_private, settings)

    if is_private or settings.public_file_mode == "Presigned URL":
        doc.file_url = f"{SERVE_PATH}?key={quote(key, safe='')}"
    else:
        doc.file_url = s3_client.public_url(key, settings)

    return {"file_name": doc.file_name, "file_url": doc.file_url}


def delete_file_data_content(doc, only_thumbnail: bool = False):
    settings = _get_settings()
    if not settings or not settings.delete_from_s3:
        return

    key = extract_key_from_url(doc.file_url)
    if not key:
        return

    try:
        s3_client.delete(key, settings)
    except Exception:
        frappe.log_error(frappe.get_traceback(), f"S3 Store delete {key}")


def extract_key_from_url(file_url: str | None) -> str | None:
    """Recover the S3 key from a file_url string we previously wrote.

    Recognises only URLs we could plausibly have produced — presigned serve
    URLs, virtual-host AWS URLs whose subdomain matches our bucket, and
    path-style URLs hosted at our configured endpoint. Foreign HTTPS URLs
    (e.g. Google Drive thumbnails) return None.
    """
    if not file_url:
        return None

    parsed = urlparse(file_url)

    if SERVE_PATH in parsed.path:
        qs = parse_qs(parsed.query)
        keys = qs.get("key", [])
        return keys[0] if keys else None

    if parsed.scheme not in ("http", "https"):
        return None

    settings = _get_settings()
    if not settings or not settings.bucket:
        return None

    bucket = settings.bucket

    if settings.endpoint_url:
        # Path-style: {endpoint_url}/{bucket}/{key}
        endpoint = urlparse(settings.endpoint_url)
        if parsed.hostname != endpoint.hostname:
            return None
        path = parsed.path.lstrip("/")
        prefix = f"{bucket}/"
        if not path.startswith(prefix):
            return None
        return path[len(prefix) :] or None

    # Virtual-host AWS: https://{bucket}.s3[.region].amazonaws.com/{key}
    if (
        parsed.hostname
        and parsed.hostname.startswith(f"{bucket}.s3")
        and parsed.hostname.endswith(".amazonaws.com")
    ):
        return parsed.path.lstrip("/") or None

    return None
