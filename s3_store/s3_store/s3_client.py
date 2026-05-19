"""Pure functions wrapping boto3. Each takes the settings doc as the last
argument so they're trivially mockable in tests and stateless at the module level."""

from __future__ import annotations

import functools
from typing import TYPE_CHECKING, BinaryIO
from urllib.parse import quote

import boto3
from botocore.client import Config

if TYPE_CHECKING:
    from .doctype.s3_store_settings.s3_store_settings import S3StoreSettings


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
    if not is_private and settings.public_file_mode == "Public ACL":
        extra["ACL"] = "public-read"
    _client(settings).upload_fileobj(fileobj, settings.bucket, key, ExtraArgs=extra)


def download_to_path(key: str, path: str, settings: "S3StoreSettings") -> None:
    _client(settings).download_file(settings.bucket, key, path)


def delete(key: str, settings: "S3StoreSettings") -> None:
    _client(settings).delete_object(Bucket=settings.bucket, Key=key)


def presigned_url(
    key: str,
    filename: str,
    expiry: int,
    settings: "S3StoreSettings",
) -> str:
    encoded = quote(filename, safe="")
    params = {
        "Bucket": settings.bucket,
        "Key": key,
        "ResponseContentDisposition": f"inline; filename*=UTF-8''{encoded}",
    }
    return _client(settings).generate_presigned_url(
        "get_object", Params=params, ExpiresIn=expiry
    )


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
    _client(settings).head_bucket(Bucket=settings.bucket)


def make_key(prefix: str, doctype: str, file_name: str) -> str:
    """Build {prefix}/{YYYY/MM/DD}/{doctype}/{token8}_{sanitized}."""
    import re
    import secrets
    from datetime import datetime, timezone

    token = secrets.token_hex(4)
    sanitized = re.sub(r"[^\w.\-]", "_", file_name or "file")
    date_part = datetime.now(timezone.utc).strftime("%Y/%m/%d")
    dt_part = re.sub(r"[^\w\-]", "_", doctype or "Misc")
    parts = [p for p in (prefix, date_part, dt_part, f"{token}_{sanitized}") if p]
    return "/".join(parts).lstrip("/")
