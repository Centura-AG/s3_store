from io import BytesIO
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from frappe.tests.utils import FrappeTestCase

from s3_store.s3_store import s3_client


def _settings(**over):
    defaults = dict(
        region="us-east-1",
        endpoint_url="",
        aws_access_key_id="AKIA",
        bucket="test-bucket",
        key_prefix="site1",
        public_file_mode="Presigned URL",
        signed_url_expiry=3600,
    )
    defaults.update(over)
    s = SimpleNamespace(**defaults)
    s.get_password = lambda field: "secret"  # noqa: ARG005
    return s


class TestS3Client(FrappeTestCase):
    def setUp(self):
        s3_client._cached_client.cache_clear()

    def test_upload_calls_boto3_with_correct_args(self):
        settings = _settings()
        with patch("s3_store.s3_store.s3_client.boto3") as mboto:
            cl = MagicMock()
            mboto.client.return_value = cl
            s3_client.upload("k", BytesIO(b"x"), "image/png", False, settings)
            cl.upload_fileobj.assert_called_once()
            args, kwargs = cl.upload_fileobj.call_args
            self.assertEqual(args[1], "test-bucket")
            self.assertEqual(args[2], "k")
            self.assertEqual(kwargs["ExtraArgs"]["ContentType"], "image/png")

    def test_upload_sets_public_acl_when_public_mode_and_not_private(self):
        settings = _settings(public_file_mode="Public ACL")
        with patch("s3_store.s3_store.s3_client.boto3") as mboto:
            cl = MagicMock()
            mboto.client.return_value = cl
            s3_client.upload("k", BytesIO(b"x"), "image/png", False, settings)
            self.assertEqual(
                cl.upload_fileobj.call_args.kwargs["ExtraArgs"]["ACL"], "public-read"
            )

    def test_upload_omits_acl_for_private(self):
        settings = _settings(public_file_mode="Public ACL")
        with patch("s3_store.s3_store.s3_client.boto3") as mboto:
            cl = MagicMock()
            mboto.client.return_value = cl
            s3_client.upload("k", BytesIO(b"x"), "image/png", True, settings)
            self.assertNotIn("ACL", cl.upload_fileobj.call_args.kwargs["ExtraArgs"])

    def test_delete_calls_delete_object(self):
        settings = _settings()
        with patch("s3_store.s3_store.s3_client.boto3") as mboto:
            cl = MagicMock()
            mboto.client.return_value = cl
            s3_client.delete("k", settings)
            cl.delete_object.assert_called_once_with(Bucket="test-bucket", Key="k")

    def test_presigned_url_passes_expiry(self):
        settings = _settings()
        with patch("s3_store.s3_store.s3_client.boto3") as mboto:
            cl = MagicMock()
            cl.generate_presigned_url.return_value = "https://signed"
            mboto.client.return_value = cl
            url = s3_client.presigned_url("k", "file.png", 600, settings)
            self.assertEqual(url, "https://signed")
            self.assertEqual(
                cl.generate_presigned_url.call_args.kwargs["ExpiresIn"], 600
            )

    def test_verify_connection_calls_head_bucket(self):
        settings = _settings()
        with patch("s3_store.s3_store.s3_client.boto3") as mboto:
            cl = MagicMock()
            mboto.client.return_value = cl
            s3_client.verify_connection(settings)
            cl.head_bucket.assert_called_once_with(Bucket="test-bucket")

    def test_make_key_format(self):
        key = s3_client.make_key("site1", "Sales Invoice", "my file.pdf")
        # site1/YYYY/MM/DD/Sales_Invoice/<token8>_my_file.pdf
        parts = key.split("/")
        self.assertEqual(parts[0], "site1")
        self.assertEqual(parts[4], "Sales_Invoice")
        self.assertRegex(parts[5], r"^[0-9a-f]{8}_my_file\.pdf$")

    def test_make_key_no_prefix(self):
        key = s3_client.make_key("", "Misc", "x.txt")
        # Should not start with /
        self.assertFalse(key.startswith("/"))

    def test_public_url_aws(self):
        settings = _settings(region="eu-central-1", endpoint_url="")
        url = s3_client.public_url("a/b.png", settings)
        self.assertEqual(
            url, "https://test-bucket.s3.eu-central-1.amazonaws.com/a/b.png"
        )

    def test_public_url_custom_endpoint(self):
        settings = _settings(endpoint_url="https://minio.example.com")
        url = s3_client.public_url("a/b.png", settings)
        self.assertEqual(url, "https://minio.example.com/test-bucket/a/b.png")

    def test_presigned_url_encodes_dangerous_filename(self):
        settings = _settings()
        with patch("s3_store.s3_store.s3_client.boto3") as mboto:
            cl = MagicMock()
            cl.generate_presigned_url.return_value = "https://signed"
            mboto.client.return_value = cl
            s3_client.presigned_url("k", 'evil"name\r\nX-Inject: header', 600, settings)
        params = cl.generate_presigned_url.call_args.kwargs["Params"]
        cd = params["ResponseContentDisposition"]
        self.assertNotIn('"', cd)
        self.assertNotIn("\r", cd)
        self.assertNotIn("\n", cd)
        self.assertIn("filename*=UTF-8''", cd)
