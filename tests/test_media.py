from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db import Base
from app.models import MediaAsset
from app.services.media import media_usage_bytes, source_key


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
