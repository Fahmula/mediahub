"""Persistence for the play history (see ``monitoring/play_history.py``)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from mediahub.db.models import PlayRecord


def upsert_plays(db: Session, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        db.merge(PlayRecord(**row))


def open_plays(db: Session) -> list[PlayRecord]:
    return list(db.scalars(select(PlayRecord).where(PlayRecord.ended_at.is_(None))))


def prune(db: Session, before: datetime) -> None:
    db.execute(
        delete(PlayRecord).where(PlayRecord.started_at < before, PlayRecord.ended_at.is_not(None))
    )


def replace_demo_plays(db: Session, rows: list[dict[str, Any]]) -> None:
    db.execute(delete(PlayRecord).where(PlayRecord.instance_id.like("demo-%")))
    upsert_plays(db, rows)
