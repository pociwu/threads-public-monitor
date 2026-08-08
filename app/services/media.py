from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from urllib.parse import urlparse

import httpx
from PIL import Image, ImageOps
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.models import ContentMedia, MediaAsset


def source_key(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def media_usage_bytes(db: Session) -> int:
    physical_files = (
        select(func.max(MediaAsset.byte_size).label("size"))
        .where(MediaAsset.local_path.is_not(None))
        .group_by(MediaAsset.local_path)
        .subquery()
    )
    return int(db.scalar(select(func.coalesce(func.sum(physical_files.c.size), 0))) or 0)


def media_usage_percent(db: Session, settings: Settings) -> float:
    return (
        (media_usage_bytes(db) / settings.max_media_bytes * 100)
        if settings.max_media_bytes
        else 100.0
    )


def media_identity(asset: MediaAsset) -> tuple[str, str]:
    if asset.sha256:
        return ("sha256", asset.sha256)
    if asset.local_path:
        return ("local_path", asset.local_path)
    return ("source_key", asset.source_key)


def image_perceptual_hash(asset: MediaAsset, media_root: Path) -> int | None:
    if asset.media_type != "image" or not asset.local_path:
        return None
    try:
        with Image.open(media_root / asset.local_path) as source:
            image = ImageOps.exif_transpose(source).convert("L").resize((17, 16))
            pixels = image.tobytes()
    except (OSError, ValueError):
        return None
    result = 0
    for row in range(16):
        offset = row * 17
        for column in range(16):
            result = (result << 1) | int(
                pixels[offset + column] > pixels[offset + column + 1]
            )
    return result


def media_equivalent(first: MediaAsset, second: MediaAsset, media_root: Path) -> bool:
    if first.sha256 and first.sha256 == second.sha256:
        return True
    if first.media_type != "image" or second.media_type != "image":
        return False
    first_hash = image_perceptual_hash(first, media_root)
    second_hash = image_perceptual_hash(second, media_root)
    return first_hash is not None and second_hash is not None and (first_hash ^ second_hash).bit_count() <= 4


def deduplicate_content_media_links(db: Session, media_root: Path | None = None) -> int:
    links = db.scalars(
        select(ContentMedia)
        .join(MediaAsset, MediaAsset.id == ContentMedia.media_id)
        .order_by(ContentMedia.content_id, ContentMedia.position, ContentMedia.id)
    ).all()
    seen: set[tuple[int, tuple[str, str]]] = set()
    kept_by_content: dict[int, list[MediaAsset]] = {}
    removed = 0
    for link in links:
        asset = db.get(MediaAsset, link.media_id)
        if asset is None:
            continue
        key = (link.content_id, media_identity(asset))
        visually_duplicated = media_root is not None and any(
            media_equivalent(asset, kept, media_root)
            for kept in kept_by_content.get(link.content_id, [])
        )
        if key in seen or visually_duplicated:
            db.delete(link)
            removed += 1
        else:
            seen.add(key)
            kept_by_content.setdefault(link.content_id, []).append(asset)
    db.flush()
    return removed


class MediaStore:
    def __init__(self, settings: Settings):
        self.settings = settings

    def register(self, db: Session, url: str, media_type: str) -> MediaAsset:
        key = source_key(url)
        existing = db.scalar(select(MediaAsset).where(MediaAsset.source_key == key))
        if existing:
            return existing
        asset = MediaAsset(source_url=url, source_key=key, media_type=media_type)
        db.add(asset)
        db.flush()
        return asset

    def download(self, db: Session, asset: MediaAsset) -> MediaAsset:
        if asset.download_status == "downloaded":
            return asset
        if media_usage_percent(db, self.settings) >= self.settings.media_stop_percent:
            asset.download_status = "capacity_blocked"
            asset.failure_reason = "媒體容量已達 95%"
            return asset

        try:
            with httpx.stream(
                "GET",
                asset.source_url,
                follow_redirects=True,
                timeout=httpx.Timeout(60, connect=20),
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.threads.com/"},
            ) as response:
                response.raise_for_status()
                length = int(response.headers.get("content-length", "0") or 0)
                if length > self.settings.max_media_file_bytes:
                    asset.download_status = "file_too_large"
                    asset.failure_reason = "單一媒體超過 500 MB"
                    return asset

                digest = hashlib.sha256()
                temp_path = self.settings.media_root / f".{asset.source_key}.part"
                total = 0
                with temp_path.open("wb") as output:
                    for chunk in response.iter_bytes(1024 * 1024):
                        total += len(chunk)
                        if total > self.settings.max_media_file_bytes:
                            output.close()
                            temp_path.unlink(missing_ok=True)
                            asset.download_status = "file_too_large"
                            asset.failure_reason = "單一媒體超過 500 MB"
                            return asset
                        if media_usage_bytes(db) + total > self.settings.max_media_bytes:
                            output.close()
                            temp_path.unlink(missing_ok=True)
                            asset.download_status = "capacity_blocked"
                            asset.failure_reason = "媒體容量上限為 100 GB"
                            return asset
                        digest.update(chunk)
                        output.write(chunk)

                sha256 = digest.hexdigest()
                duplicate = db.scalar(
                    select(MediaAsset).where(MediaAsset.sha256 == sha256, MediaAsset.id != asset.id)
                )
                if duplicate and duplicate.local_path:
                    temp_path.unlink(missing_ok=True)
                    asset.sha256 = sha256
                    asset.local_path = duplicate.local_path
                    asset.byte_size = duplicate.byte_size
                    asset.mime_type = duplicate.mime_type
                    asset.download_status = "downloaded"
                    return duplicate

                content_type = response.headers.get("content-type", "").split(";", 1)[0] or None
                extension = (
                    mimetypes.guess_extension(content_type or "")
                    or Path(urlparse(str(response.url)).path).suffix
                )
                if not extension or len(extension) > 8:
                    extension = ".bin"
                destination_dir = self.settings.media_root / sha256[:2] / sha256[2:4]
                destination_dir.mkdir(parents=True, exist_ok=True)
                destination = destination_dir / f"{sha256}{extension}"
                temp_path.replace(destination)
                asset.sha256 = sha256
                asset.mime_type = content_type
                asset.local_path = str(destination.relative_to(self.settings.media_root))
                asset.byte_size = total
                asset.download_status = "downloaded"
                asset.failure_reason = None
                return asset
        except (httpx.HTTPError, OSError, ValueError) as exc:
            asset.download_status = "failed"
            asset.failure_reason = str(exc)[:500]
            return asset
