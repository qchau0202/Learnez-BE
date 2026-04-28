"""Cloudinary helpers for course materials and student file management."""

from __future__ import annotations

import os
from urllib.parse import unquote, urlparse

import time

import cloudinary
import cloudinary.uploader
import cloudinary.utils

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
    """Upload bytes as Cloudinary auto-detected resource; returns uploader response."""
    ensure_cloudinary_configured()
    # Auto-detect keeps Cloudinary behavior consistent across files and dashboard visibility.
    return cloudinary.uploader.upload(
        payload,
        resource_type="auto",
        folder=folder,
        public_id=filename,
        overwrite=False,
        unique_filename=True,
        use_filename=True,
        filename=filename,
        format=None,
    )


def delete_public_id(public_id: str, *, resource_type: str | None = None) -> dict:
    ensure_cloudinary_configured()
    rt = (resource_type or "raw").strip() or "raw"
    return cloudinary.uploader.destroy(public_id, resource_type=rt, invalidate=True)


def signed_download_url(
    public_id: str,
    *,
    resource_type: str | None = None,
    ttl_seconds: int = 3600,
    attachment: bool = False,
) -> str:
    """Return a short-lived signed delivery URL.

    Works even if the account has "Restricted media types" enabled (PDF/ZIP/etc.),
    because the signature authenticates the request server-to-server.
    """
    ensure_cloudinary_configured()
    expires_at = int(time.time()) + max(60, ttl_seconds)
    rt = (resource_type or "raw").strip() or "raw"
    # Prefer private_download_url for better compatibility with restricted delivery.
    try:
        return cloudinary.utils.private_download_url(
            public_id,
            resource_type=rt,
            type="upload",
            expires_at=expires_at,
            attachment=attachment,
        )
    except Exception:
        url, _options = cloudinary.utils.cloudinary_url(
            public_id,
            resource_type=rt,
            type="upload",
            secure=True,
            sign_url=True,
            expires_at=expires_at,
            attachment=attachment,
        )
        return url


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
        # Keep extension when present. In this project, uploaded public_id often
        # includes the original extension; stripping it can break signed URLs.
        return tail
    except Exception:
        return None


def cloudinary_enabled() -> bool:
    return bool(
        (os.getenv("CLOUDINARY_CLOUD_NAME") or "").strip()
        and (os.getenv("CLOUDINARY_API_KEY") or "").strip()
        and (os.getenv("CLOUDINARY_API_SECRET") or "").strip()
    )
