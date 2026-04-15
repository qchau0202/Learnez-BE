"""Cloudinary helpers for course materials and student file management."""

from __future__ import annotations

import os
from urllib.parse import unquote, urlparse

import cloudinary
import cloudinary.uploader

_configured = False


def _required_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def ensure_cloudinary_configured() -> None:
    global _configured
    if _configured:
        return
    cloud_name = _required_env("CLOUDINARY_CLOUD_NAME")
    api_key = _required_env("CLOUDINARY_API_KEY")
    api_secret = _required_env("CLOUDINARY_API_SECRET")
    cloudinary.config(cloud_name=cloud_name, api_key=api_key, api_secret=api_secret, secure=True)
    _configured = True


def upload_bytes(
    payload: bytes,
    *,
    folder: str,
    filename: str,
    content_type: str | None = None,
) -> dict:
    """Upload bytes as Cloudinary raw resource; returns uploader response."""
    ensure_cloudinary_configured()
    # `resource_type=raw` works for docs/images while avoiding video pipelines.
    return cloudinary.uploader.upload(
        payload,
        resource_type="raw",
        folder=folder,
        public_id=filename,
        overwrite=False,
        unique_filename=True,
        use_filename=True,
        filename=filename,
        format=None,
    )


def delete_public_id(public_id: str) -> dict:
    ensure_cloudinary_configured()
    return cloudinary.uploader.destroy(public_id, resource_type="raw", invalidate=True)


def public_id_from_url(file_url: str | None) -> str | None:
    """Parse Cloudinary public_id from secure_url."""
    if not file_url:
        return None
    try:
        p = urlparse(file_url)
        path = unquote(p.path or "")
        # /<cloud>/raw/upload/v123/folder/name.ext
        marker = "/upload/"
        if marker not in path:
            return None
        tail = path.split(marker, 1)[1]
        if tail.startswith("v"):
            parts = tail.split("/", 1)
            tail = parts[1] if len(parts) > 1 else ""
        if not tail:
            return None
        if "." in tail:
            tail = tail.rsplit(".", 1)[0]
        return tail
    except Exception:
        return None


def cloudinary_enabled() -> bool:
    return bool(
        (os.getenv("CLOUDINARY_CLOUD_NAME") or "").strip()
        and (os.getenv("CLOUDINARY_API_KEY") or "").strip()
        and (os.getenv("CLOUDINARY_API_SECRET") or "").strip()
    )
