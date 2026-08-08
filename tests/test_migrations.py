from datetime import date
from importlib import import_module

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db import Base
from app.models import (
    Account,
    RelationshipChange,
    RelationshipMember,
    RelationshipScan,
    RelationshipScanMember,
)


def test_empty_follower_scan_repair_restores_last_known_members(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        account = Account(username="example", status="active", follower_count=51)
        db.add(account)
        db.flush()
        member = RelationshipMember(
            account_id=account.id,
            relationship_type="followers",
            username="alice",
            active=False,
        )
        previous = RelationshipScan(
            account_id=account.id,
            relationship_type="followers",
            scan_date=date(2026, 8, 7),
            status="failed",
            collected_count=1,
        )
        empty = RelationshipScan(
            account_id=account.id,
            relationship_type="followers",
            scan_date=date(2026, 8, 8),
            status="complete",
            collected_count=0,
        )
        db.add_all([member, previous, empty])
        db.flush()
        db.add(RelationshipScanMember(scan_id=previous.id, member_id=member.id))
        db.add(
            RelationshipChange(
                account_id=account.id,
                scan_id=empty.id,
                member_id=member.id,
                relationship_type="followers",
                change_type="removed",
                observed_date=empty.scan_date,
            )
        )
        db.commit()
        empty_id = empty.id
        member_id = member.id

    migration = import_module("migrations.versions.0005_repair_empty_follower_scans")
    monkeypatch.setattr(migration.op, "get_bind", lambda: engine)
    migration.upgrade()

    with Session(engine) as db:
        assert db.get(RelationshipScan, empty_id).status == "failed"
        assert db.get(RelationshipMember, member_id).active is True
        assert db.scalar(select(RelationshipChange)) is None
