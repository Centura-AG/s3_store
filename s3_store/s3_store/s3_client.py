"""Pure functions wrapping boto3. Each takes the settings doc as the last
argument so they're trivially mockable in tests and stateless at the module level."""

from __future__ import annotations

import functools
import logging
from typing import TYPE_CHECKING, BinaryIO

import boto3
from botocore.client import Config

if TYPE_CHECKING:
    from .doctype.s3_store_settings.s3_store_settings import S3StoreSettings

logger = logging.getLogger(__name__)


@functools.lru_cache(maxsize=4)
def _cached_client(endpoint_url: str, region: str, access_key_id: str, secret_key: str):
    kwargs = {
        "region_name": region or "us-east-1",
        "aws_access_key_id": access_key_id,
        "aws_secret_access_key": secret_key,
        "config": Config(signature_version="s3v4"),
    }
    if endpoint_url:
        kwargs["endpoint_url"] = endpoint_url
    return boto3.client("s3", **kwargs)


def _client(settings: "S3StoreSettings"):
    return _cached_client(
        settings.endpoint_url or "",
        settings.region or "us-east-1",
        settings.get_password("aws_access_key_id"),
        settings.get_password("aws_secret_access_key"),
    )


def upload(
    key: str,
    fileobj: BinaryIO,
    content_type: str,
    is_private: bool,
    settings: "S3StoreSettings",
) -> None:
    extra = {"ContentType": content_type or "application/octet-stream"}
    logger.debug("S3 upload: bucket=%s key=%s", settings.bucket, key)
    _client(settings).upload_fileobj(fileobj, settings.bucket, key, ExtraArgs=extra)
    logger.debug("S3 upload complete: key=%s", key)


def download_to_path(key: str, path: str, settings: "S3StoreSettings") -> None:
    logger.debug("S3 download: bucket=%s key=%s -> %s", settings.bucket, key, path)
    _client(settings).download_file(settings.bucket, key, path)
    logger.debug("S3 download complete: key=%s", key)


def delete(key: str, settings: "S3StoreSettings") -> None:
    logger.debug("S3 delete: bucket=%s key=%s", settings.bucket, key)
    _client(settings).delete_object(Bucket=settings.bucket, Key=key)
    logger.debug("S3 delete complete: key=%s", key)


def clear_client_cache() -> None:
    """Invalidate all cached boto3 clients. Call after credential rotation."""
    _cached_client.cache_clear()


def get_object(key: str, settings: "S3StoreSettings") -> dict:
    return _client(settings).get_object(Bucket=settings.bucket, Key=key)


def public_url(key: str, settings: "S3StoreSettings") -> str:
    if settings.endpoint_url:
        base = settings.endpoint_url.rstrip("/")
        return f"{base}/{settings.bucket}/{key}"
    region = settings.region or "us-east-1"
    if region == "us-east-1":
        return f"https://{settings.bucket}.s3.amazonaws.com/{key}"
    return f"https://{settings.bucket}.s3.{region}.amazonaws.com/{key}"


def head(key: str, settings: "S3StoreSettings") -> dict:
    return _client(settings).head_object(Bucket=settings.bucket, Key=key)


def verify_connection(settings: "S3StoreSettings") -> None:
    import secrets

    client = _client(settings)
    client.head_bucket(Bucket=settings.bucket)
    # Probe write + delete to catch missing PutObject/DeleteObject IAM permissions.
    probe_key = f".s3_store_probe_{secrets.token_hex(8)}"
    client.put_object(Bucket=settings.bucket, Key=probe_key, Body=b"")
    client.delete_object(Bucket=settings.bucket, Key=probe_key)


def make_key(pattern: str, doctype: str, file_name: str) -> str:
    """Build an S3 key from a pattern template.

    Variables: {date} YYYY/MM/DD, {doctype}, {token} 8-char hex, {filename}.
    Default pattern: {date}/{doctype}/{token}_{filename}.
    A pattern without '{' is treated as a plain prefix prepended to the default.
    """
    import re
    import secrets
    from datetime import datetime, timezone

    token = secrets.token_hex(4)
    # Explicit ASCII allow-list: \w matches Unicode in Python, so pin to ASCII only.
    sanitized = re.sub(r"[^a-zA-Z0-9.\-_]", "_", file_name or "file")
    date_part = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    dt_part = re.sub(r"[^a-zA-Z0-9\-_]", "_", doctype or "Misc")

    template = (
        f"{pattern.rstrip('/')}/{{date}}/{{doctype}}/{{token}}_{{filename}}"
        if pattern and "{" not in pattern
        else (pattern or "{date}/{doctype}/{token}_{filename}")
    )

    return template.format(
        date=date_part,
        doctype=dt_part,
        token=token,
        filename=sanitized,
    ).lstrip("/")
