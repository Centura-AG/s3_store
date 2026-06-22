from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from s3_store.s3_store import api


class TestServe(FrappeTestCase):
    def test_missing_key_raises_permission_error(self):
        with self.assertRaises(frappe.PermissionError):
            api.serve("")

    def test_unknown_key_raises_does_not_exist(self):
        with (
            patch.object(frappe.db, "get_value", return_value=None),
            self.assertRaises(frappe.DoesNotExistError),
        ):
            api.serve("nokey")

    def test_valid_public_key_proxies_content(self):
        fake_doc = MagicMock()
        fake_doc.is_private = 0
        fake_doc.file_name = "x.png"

        body = MagicMock()
        body.read.return_value = b"filebytes"

        with (
            patch.object(frappe.db, "get_value", return_value="FILE-1"),
            patch.object(frappe, "get_doc", return_value=fake_doc),
            patch.object(frappe, "get_cached_doc") as gcd,
            patch(
                "s3_store.s3_store.s3_client.get_object",
                return_value={"Body": body, "ContentType": "image/png"},
            ),
        ):
            gcd.return_value = MagicMock(enabled=1)
            frappe.local.response = {}
            api.serve("somekey")
            self.assertEqual(frappe.local.response["type"], "download")
            self.assertEqual(frappe.local.response["filecontent"], b"filebytes")
            self.assertEqual(frappe.local.response["content_type"], "image/png")
            self.assertEqual(frappe.local.response["filename"], "x.png")
        # The streamed body must be closed once it has been read.
        body.close.assert_called_once()

    def test_lookup_uses_exact_match_with_encoded_key(self):
        # Regression: previously this did a LIKE '%key=KEY%' scan. Now it
        # builds the full encoded URL and asks for an exact match.
        fake_doc = MagicMock(is_private=0, file_name="x.png")
        body = MagicMock()
        body.read.return_value = b""
        with (
            patch.object(frappe.db, "get_value", return_value="FILE-1") as gv,
            patch.object(frappe, "get_doc", return_value=fake_doc),
            patch.object(frappe, "get_cached_doc") as gcd,
            patch(
                "s3_store.s3_store.s3_client.get_object",
                return_value={"Body": body, "ContentType": "text/plain"},
            ),
        ):
            gcd.return_value = MagicMock(enabled=1)
            frappe.local.response = {}
            api.serve("p/2026/k.txt")
        # The filter must be an exact-match dict, not a LIKE clause, and the
        # key must have been URL-encoded into the expected URL.
        filt = gv.call_args.args[1]
        self.assertEqual(
            filt,
            {
                "file_url": "/api/method/s3_store.s3_store.api.serve?key=p%2F2026%2Fk.txt"
            },
        )

    def test_wildcard_key_uses_exact_match_and_raises_not_found(self):
        with (
            patch.object(frappe.db, "get_value", return_value=None) as gv,
            self.assertRaises(frappe.DoesNotExistError),
        ):
            api.serve("%")
        filt = gv.call_args.args[1]
        self.assertEqual(
            filt,
            {"file_url": "/api/method/s3_store.s3_store.api.serve?key=%25"},
        )

    def test_public_file_always_checks_permission(self):
        fake_doc = MagicMock()
        fake_doc.is_private = 0
        fake_doc.check_permission.side_effect = frappe.PermissionError("no access")

        with (
            patch.object(frappe.db, "get_value", return_value="FILE-1"),
            patch.object(frappe, "get_doc", return_value=fake_doc),
            self.assertRaises(frappe.PermissionError),
        ):
            api.serve("somekey")

        fake_doc.check_permission.assert_called_once_with("read")


class TestStartMigration(FrappeTestCase):
    def test_calls_only_for_with_admin_roles(self):
        # frappe.only_for is a no-op during tests (local.flags.in_test),
        # so we verify the guard was placed with the correct roles
        # rather than that it actually raised.
        with (
            patch.object(frappe, "only_for") as only_for,
            patch.object(frappe, "enqueue"),
        ):
            api.start_migration()
        only_for.assert_called_once_with(["System Manager", "Administrator"])

    def test_creates_log_and_enqueues(self):
        with patch.object(frappe, "enqueue") as eq:
            result = api.start_migration()
        self.assertTrue(result["log"])
        eq.assert_called_once()
        log = frappe.get_doc("S3 Migration Log", result["log"])
        self.assertEqual(log.status, "Queued")
