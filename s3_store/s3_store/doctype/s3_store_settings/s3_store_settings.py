import re

import frappe
from frappe import _
from frappe.model.document import Document

_BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{1,61}[a-z0-9]$")


class S3StoreSettings(Document):
    def validate(self):
        if not self.enabled:
            return

        if not self.bucket:
            frappe.throw(_("Bucket is required when S3 Store is enabled"))
        if not _BUCKET_RE.match(self.bucket or ""):
            frappe.throw(
                _(
                    "Bucket name must be 3–63 characters, contain only lowercase "
                    "letters, numbers, or hyphens, and cannot start or end with a hyphen."
                )
            )
        if not self.get_password(
            "aws_access_key_id", raise_exception=False
        ) or not self.get_password("aws_secret_access_key", raise_exception=False):
            frappe.throw(_("AWS credentials are required when S3 Store is enabled"))

        from s3_store.s3_store import s3_client

        try:
            s3_client.verify_connection(self)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "S3 verify_connection")
            frappe.throw(_("S3 connection check failed. See Error Log for details."))

    def on_update(self):
        from s3_store.s3_store import s3_client

        s3_client.clear_client_cache()

    def get_ignored_doctypes(self) -> set[str]:
        if not self.ignored_doctypes:
            return set()
        return {
            line.strip() for line in self.ignored_doctypes.splitlines() if line.strip()
        }
