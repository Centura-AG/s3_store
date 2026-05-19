import frappe
from frappe import _
from frappe.model.document import Document


class S3StoreSettings(Document):
    def validate(self):
        if not self.enabled:
            return

        if not self.bucket:
            frappe.throw(_("Bucket is required when S3 Store is enabled"))
        if not self.get_password(
            "aws_access_key_id", raise_exception=False
        ) or not self.get_password("aws_secret_access_key", raise_exception=False):
            frappe.throw(_("AWS credentials are required when S3 Store is enabled"))

        if self.signed_url_expiry:
            self.signed_url_expiry = max(60, min(int(self.signed_url_expiry), 604800))

        from s3_store.s3_store import s3_client

        try:
            s3_client.verify_connection(self)
        except Exception:
            frappe.log_error(frappe.get_traceback(), "S3 verify_connection")
            frappe.throw(_("S3 connection check failed. See Error Log for details."))

    def get_ignored_doctypes(self) -> set[str]:
        if not self.ignored_doctypes:
            return set()
        return {
            line.strip() for line in self.ignored_doctypes.splitlines() if line.strip()
        }
