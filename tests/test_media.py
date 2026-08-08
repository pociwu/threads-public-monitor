from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import Account, Content, ContentMedia, MediaAsset
from app.services.media import deduplicate_content_media_links, media_usage_bytes, source_key


def test_source_key_is_stable() -> None:
    assert source_key("https://example.test/a.jpg") == source_key("https://example.test/a.jpg")
    assert source_key("https://example.test/a.jpg") != source_key("https://example.test/b.jpg")


def test_media_usage_counts_shared_physical_file_once() -> None:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                MediaAsset(
                    source_url="https://example.test/1",
                    source_key="1" * 64,
                    media_type="image",
                    local_path="aa/shared.jpg",
                    byte_size=123,
                    download_status="downloaded",
                ),
                MediaAsset(
                    source_url="https://example.test/2",
                    source_key="2" * 64,
                    media_type="image",
                    local_path="aa/shared.jpg",
                    byte_size=123,
                    download_status="downloaded",
                ),
            ]
        )
        db.commit()
        assert media_usage_bytes(db) == 123


def test_existing_duplicate_content_media_links_are_removed() -> None:
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        account = Account(username="example")
        db.add(account)
        db.flush()
        content = Content(
            threads_id="post-1",
            account_id=account.id,
            author_username="example",
            content_type="post",
            source_url="https://www.threads.com/@example/post/post-1",
        )
        first = MediaAsset(
            source_url="https://cdn.example/one.mp4",
            source_key="one",
            media_type="video",
            sha256="a" * 64,
            local_path="aa/shared.mp4",
            download_status="downloaded",
        )
        second = MediaAsset(
            source_url="https://cdn.example/two.mp4",
            source_key="two",
            media_type="video",
            sha256="a" * 64,
            local_path="aa/shared.mp4",
            download_status="downloaded",
        )
        db.add_all([content, first, second])
        db.flush()
        db.add_all(
            [
                ContentMedia(content_id=content.id, media_id=first.id, position=0),
                ContentMedia(content_id=content.id, media_id=second.id, position=1),
            ]
        )
        db.commit()

        assert deduplicate_content_media_links(db) == 1
        assert len(db.scalars(select(ContentMedia)).all()) == 1


def test_different_resolution_copies_are_visually_deduplicated(tmp_path) -> None:
    def write_pgm(path, size: int) -> None:
        pixels = bytearray()
        for y in range(size):
            for x in range(size):
                pixels.append(240 if (x < size // 2) == (y < size // 2) else 20)
        path.write_bytes(f"P5\n{size} {size}\n255\n".encode() + pixels)

    write_pgm(tmp_path / "small.pgm", 32)
    write_pgm(tmp_path / "large.pgm", 128)
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        account = Account(username="example")
        db.add(account)
        db.flush()
        content = Content(
            threads_id="post-visual",
            account_id=account.id,
            author_username="example",
            content_type="post",
            source_url="https://www.threads.com/@example/post/post-visual",
        )
        first = MediaAsset(
            source_url="https://cdn.example/small.jpg",
            source_key="small",
            media_type="image",
            sha256="1" * 64,
            local_path="small.pgm",
            download_status="downloaded",
        )
        second = MediaAsset(
            source_url="https://cdn.example/large.jpg",
            source_key="large",
            media_type="image",
            sha256="2" * 64,
            local_path="large.pgm",
            download_status="downloaded",
        )
        db.add_all([content, first, second])
        db.flush()
        db.add_all(
            [
                ContentMedia(content_id=content.id, media_id=first.id, position=0),
                ContentMedia(content_id=content.id, media_id=second.id, position=1),
            ]
        )
        db.commit()

        assert deduplicate_content_media_links(db, tmp_path) == 1
        assert len(db.scalars(select(ContentMedia)).all()) == 1
