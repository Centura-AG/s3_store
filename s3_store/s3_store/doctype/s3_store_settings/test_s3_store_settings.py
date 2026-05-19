from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase


class TestS3StoreSettings(FrappeTestCase):
    def setUp(self):
        self.settings = frappe.get_single("S3 Store Settings")
        self.settings.enabled = 0
        self.settings.save(ignore_permissions=True)

    def test_validate_skipped_when_disabled(self):
        self.settings.enabled = 0
        self.settings.save(ignore_permissions=True)

    def test_validate_requires_bucket_when_enabled(self):
        self.settings.enabled = 1
        self.settings.bucket = ""
        self.settings.aws_access_key_id = "AKIA"
        self.settings.aws_secret_access_key = "secret"
        with self.assertRaises(frappe.ValidationError):
            self.settings.save(ignore_permissions=True)

    def test_validate_requires_credentials_when_enabled(self):
        self.settings.enabled = 1
        self.settings.bucket = "test-bucket"
        self.settings.aws_access_key_id = ""
        with self.assertRaises(frappe.ValidationError):
            self.settings.save(ignore_permissions=True)

    def test_validate_calls_verify_connection(self):
        self.settings.enabled = 1
        self.settings.bucket = "test-bucket"
        self.settings.aws_access_key_id = "AKIA"
        self.settings.aws_secret_access_key = "secret"
        with patch("s3_store.s3_store.s3_client.verify_connection") as mock_verify:
            self.settings.save(ignore_permissions=True)
            mock_verify.assert_called_once()

    def test_validate_raises_on_bad_credentials(self):
        self.settings.enabled = 1
        self.settings.bucket = "test-bucket"
        self.settings.aws_access_key_id = "AKIA"
        self.settings.aws_secret_access_key = "secret"
        with patch(
            "s3_store.s3_store.s3_client.verify_connection",
            side_effect=RuntimeError("nope"),
        ):
            with self.assertRaises(frappe.ValidationError):
                self.settings.save(ignore_permissions=True)

    def test_get_ignored_doctypes(self):
        self.settings.ignored_doctypes = "Foo\nBar\n\n  Baz  "
        self.settings.save(ignore_permissions=True)
        self.assertEqual(self.settings.get_ignored_doctypes(), {"Foo", "Bar", "Baz"})
