import frappe
from frappe.tests.utils import FrappeTestCase


class TestS3MigrationLog(FrappeTestCase):
    def test_insert_and_status_progression(self):
        log = frappe.get_doc({"doctype": "S3 Migration Log", "status": "Queued"})
        log.insert(ignore_permissions=True)
        self.assertEqual(log.status, "Queued")
        log.status = "Running"
        log.save(ignore_permissions=True)
        log.status = "Completed"
        log.save(ignore_permissions=True)
        self.assertEqual(log.status, "Completed")

    def test_non_system_manager_cannot_read(self):
        log = frappe.get_doc({"doctype": "S3 Migration Log", "status": "Queued"})
        log.insert(ignore_permissions=True)
        # Switch to a guest-like user and confirm permission denied
        user = "test-no-perms@example.com"
        if not frappe.db.exists("User", user):
            frappe.get_doc(
                {
                    "doctype": "User",
                    "email": user,
                    "first_name": "NoPerms",
                    "send_welcome_email": 0,
                }
            ).insert(ignore_permissions=True)
        frappe.set_user(user)
        try:
            has_perm = frappe.has_permission(
                "S3 Migration Log", "read", log.name, user=user
            )
            self.assertFalse(has_perm)
        finally:
            frappe.set_user("Administrator")
