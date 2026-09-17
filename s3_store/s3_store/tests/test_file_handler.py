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


class TestGetSettings(FrappeTestCase):
    def test_returns_none_when_the_single_is_missing(self):
        with patch.object(
            frappe, "get_cached_doc", side_effect=frappe.DoesNotExistError
        ):
            self.assertIsNone(file_handler._get_settings())

    def test_returns_none_when_disabled(self):
        with patch.object(
            frappe, "get_cached_doc", return_value=_settings_doc(enabled=0)
        ):
            self.assertIsNone(file_handler._get_settings())

    def test_returns_the_doc_when_enabled(self):
        settings = _settings_doc()
        with patch.object(frappe, "get_cached_doc", return_value=settings):
            self.assertIs(file_handler._get_settings(), settings)


class TestWriteFileEdgeCases(FrappeTestCase):
    def test_text_content_is_encoded_before_upload(self):
        doc = _fake_doc(_content="hello", content_type=None, file_name="hello.txt")
        with (
            patch.object(file_handler, "_get_settings", return_value=_settings_doc()),
            patch("s3_store.s3_store.s3_client.upload") as up,
        ):
            file_handler.write_file(doc)
        self.assertEqual(up.call_args.args[1].read(), b"hello")
        self.assertEqual(up.call_args.args[2], "text/plain")

    def test_upload_failure_is_logged_and_reraised(self):
        doc = _fake_doc()
        with (
            patch.object(file_handler, "_get_settings", return_value=_settings_doc()),
            patch(
                "s3_store.s3_store.s3_client.upload",
                side_effect=RuntimeError("no bucket"),
            ),
            patch.object(frappe, "log_error") as log,
            self.assertRaises(RuntimeError),
        ):
            file_handler.write_file(doc)
        self.assertIn("S3 Store upload", log.call_args.args[1])


class TestDeleteFileDataContent(FrappeTestCase):
    def _doc(self, file_url):
        return SimpleNamespace(file_url=file_url)

    def test_does_nothing_without_a_recognisable_key(self):
        with (
            patch.object(file_handler, "_get_settings", return_value=_settings_doc()),
            patch("s3_store.s3_store.s3_client.delete") as delete,
        ):
            file_handler.delete_file_data_content(self._doc("/files/local.txt"))
        delete.assert_not_called()


class TestExtractKeyFromUrl(FrappeTestCase):
    def test_returns_none_for_a_serve_url_without_a_key(self):
        self.assertIsNone(
            file_handler.extract_key_from_url(f"{file_handler.SERVE_PATH}?other=1")
        )

    def test_returns_none_for_a_local_path(self):
        self.assertIsNone(file_handler.extract_key_from_url("/files/local.txt"))

    def test_returns_none_when_s3_is_disabled(self):
        with patch.object(file_handler, "_get_settings", return_value=None):
            self.assertIsNone(
                file_handler.extract_key_from_url("https://b.s3.amazonaws.com/p/k.txt")
            )

    def test_path_style_url_at_the_configured_endpoint(self):
        settings = _settings_doc(endpoint_url="https://minio.example.com")
        with patch.object(file_handler, "_get_settings", return_value=settings):
            self.assertEqual(
                file_handler.extract_key_from_url(
                    "https://minio.example.com/b/p/2026/k.txt"
                ),
                "p/2026/k.txt",
            )

    def test_path_style_url_from_another_host_is_rejected(self):
        settings = _settings_doc(endpoint_url="https://minio.example.com")
        with patch.object(file_handler, "_get_settings", return_value=settings):
            self.assertIsNone(
                file_handler.extract_key_from_url("https://other.example.com/b/p/k.txt")
            )

    def test_path_style_url_for_another_bucket_is_rejected(self):
        settings = _settings_doc(endpoint_url="https://minio.example.com")
        with patch.object(file_handler, "_get_settings", return_value=settings):
            self.assertIsNone(
                file_handler.extract_key_from_url(
                    "https://minio.example.com/other-bucket/p/k.txt"
                )
            )

    def test_path_style_url_without_a_key_is_rejected(self):
        settings = _settings_doc(endpoint_url="https://minio.example.com")
        with patch.object(file_handler, "_get_settings", return_value=settings):
            self.assertIsNone(
                file_handler.extract_key_from_url("https://minio.example.com/b/")
            )
