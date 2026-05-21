from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from s3_store.s3_store import file_handler


def _fake_doc(**over):
    defaults = dict(
        _content=b"hello",
        file_name="hello.txt",
        is_private=0,
        attached_to_doctype="ToDo",
        content_type="text/plain",
    )
    defaults.update(over)
    doc = SimpleNamespace(**defaults)
    doc.save_file_on_filesystem = MagicMock(
        return_value={"file_name": doc.file_name, "file_url": "/files/hello.txt"}
    )
    return doc


def _settings_doc(**over):
    defaults = dict(
        enabled=1,
        bucket="b",
        key_prefix="p",
        delete_from_s3=1,
        region="us-east-1",
        endpoint_url="",
        aws_access_key_id="AKIA",
    )
    defaults.update(over)
    s = SimpleNamespace(**defaults)
    s.get_password = lambda field: "secret"  # noqa: ARG005
    s.get_ignored_doctypes = lambda: set(over.get("ignored", []))
    return s


class TestFileHandler(FrappeTestCase):
    def test_falls_back_when_disabled(self):
        doc = _fake_doc()
        with patch.object(file_handler, "_get_settings", return_value=None):
            result = file_handler.write_file(doc)
        doc.save_file_on_filesystem.assert_called_once()
        self.assertEqual(result["file_url"], "/files/hello.txt")

    def test_uploads_when_enabled(self):
        doc = _fake_doc()
        settings = _settings_doc()
        with (
            patch.object(file_handler, "_get_settings", return_value=settings),
            patch("s3_store.s3_store.s3_client.upload") as up,
        ):
            result = file_handler.write_file(doc)
        up.assert_called_once()
        self.assertIn(
            "/api/method/s3_store.s3_store.api.serve?key=", result["file_url"]
        )
        doc.save_file_on_filesystem.assert_not_called()

    def test_ignored_doctype_falls_back(self):
        doc = _fake_doc(attached_to_doctype="Ignored DT")
        settings = _settings_doc(ignored=["Ignored DT"])
        with (
            patch.object(file_handler, "_get_settings", return_value=settings),
            patch("s3_store.s3_store.s3_client.upload") as up,
        ):
            result = file_handler.write_file(doc)
        up.assert_not_called()
        doc.save_file_on_filesystem.assert_called_once()
        self.assertEqual(result["file_url"], "/files/hello.txt")

    def test_extract_key_from_serve_url(self):
        key = file_handler.extract_key_from_url(
            "/api/method/s3_store.s3_store.api.serve?key=site1%2F2026%2F01%2F01%2FFile%2Fdeadbeef_a.png"
        )
        self.assertEqual(key, "site1/2026/01/01/File/deadbeef_a.png")

    def test_extract_key_from_none(self):
        self.assertIsNone(file_handler.extract_key_from_url(None))
        self.assertIsNone(file_handler.extract_key_from_url(""))

    def test_delete_calls_s3_delete(self):
        doc = SimpleNamespace(
            file_url="/api/method/s3_store.s3_store.api.serve?key=foo"
        )
        settings = _settings_doc()
        with (
            patch.object(file_handler, "_get_settings", return_value=settings),
            patch("s3_store.s3_store.s3_client.delete") as dl,
        ):
            file_handler.delete_file_data_content(doc)
        dl.assert_called_once_with("foo", settings)

    def test_delete_swallows_errors(self):
        doc = SimpleNamespace(
            file_url="/api/method/s3_store.s3_store.api.serve?key=foo"
        )
        settings = _settings_doc()
        with (
            patch.object(file_handler, "_get_settings", return_value=settings),
            patch(
                "s3_store.s3_store.s3_client.delete", side_effect=RuntimeError("nope")
            ),
            patch.object(frappe, "log_error") as le,
        ):
            file_handler.delete_file_data_content(doc)
        le.assert_called_once()

    def test_delete_skipped_when_setting_off(self):
        doc = SimpleNamespace(
            file_url="/api/method/s3_store.s3_store.api.serve?key=foo"
        )
        settings = _settings_doc(delete_from_s3=0)
        with (
            patch.object(file_handler, "_get_settings", return_value=settings),
            patch("s3_store.s3_store.s3_client.delete") as dl,
        ):
            file_handler.delete_file_data_content(doc)
        dl.assert_not_called()
