"""Persistence for the play history (see ``monitoring/play_history.py``)."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import ColumnElement, and_, delete, func, or_, select
from sqlalchemy.orm import Session

from mediahub.db.models import PlayRecord

CLEAN = "ok"


def _with_problems() -> ColumnElement[bool]:
    return PlayRecord.outcome != CLEAN


def upsert_plays(db: Session, rows: list[dict[str, Any]]) -> None:
    for row in rows:
        db.merge(PlayRecord(**row))


def open_plays(db: Session) -> list[PlayRecord]:
    return list(db.scalars(select(PlayRecord).where(PlayRecord.ended_at.is_(None))))


def list_plays(
    db: Session,
    *,
    only_problems: bool,
    limit: int,
    before: tuple[datetime, str] | None = None,
) -> list[PlayRecord]:
    """Newest first. ``before`` is the (started_at, id) of the last row of the previous page."""
    query = select(PlayRecord).order_by(PlayRecord.started_at.desc(), PlayRecord.id.desc())
    if only_problems:
        query = query.where(_with_problems())
    if before is not None:
        started_at, play_id = before
        query = query.where(
            or_(
                PlayRecord.started_at < started_at,
                and_(PlayRecord.started_at == started_at, PlayRecord.id < play_id),
            )
        )
    return list(db.scalars(query.limit(limit)))


def count_plays(db: Session) -> tuple[int, int]:
    """(plays with problems, all plays)."""
    problems = db.scalar(select(func.count()).select_from(PlayRecord).where(_with_problems()))
    total = db.scalar(select(func.count()).select_from(PlayRecord))
    return problems or 0, total or 0


def recent_problem_count(db: Session, since: datetime) -> int:
    query = select(func.count()).select_from(PlayRecord)
    return db.scalar(query.where(_with_problems(), PlayRecord.started_at >= since)) or 0


def prune(db: Session, before: datetime) -> None:
    db.execute(
        delete(PlayRecord).where(PlayRecord.started_at < before, PlayRecord.ended_at.is_not(None))
    )


def replace_demo_plays(db: Session, rows: list[dict[str, Any]]) -> None:
    db.execute(delete(PlayRecord).where(PlayRecord.instance_id.like("demo-%")))
    upsert_plays(db, rows)
