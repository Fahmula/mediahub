"""Pair Emby sessions with the InfiniDysk read of the same file."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from mediahub.models.infinidysk import ActiveRead
from mediahub.models.snapshot import ServerSnapshot, ServiceSnapshot


def _played_name(file_name: str) -> str:
    """Emby plays STRM files as ``<name>.strm``; unrenamed ones keep InfiniDysk's file name."""
    return file_name.lower().removesuffix(".strm")


def _last_segment(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1].lower()


@dataclass
class ReadIndex:
    by_target: dict[str, ActiveRead] = field(default_factory=dict)
    by_name: dict[str, ActiveRead] = field(default_factory=dict)

    def find(self, strm_target: str | None, file_name: str | None) -> ActiveRead | None:
        read = self.by_target.get(strm_target.lower()) if strm_target else None
        if read is None and file_name:
            read = self.by_name.get(_played_name(file_name))
        return read


def build_read_index(services: Iterable[ServiceSnapshot]) -> ReadIndex:
    """Every read of a live InfiniDysk feed; without a feed there are no reads or problems."""
    index = ReadIndex()

    def keep(reads: dict[str, ActiveRead], key: str, read: ActiveRead) -> None:
        if key not in reads or (reads[key].idle and not read.idle):
            reads[key] = read

    for service in services:
        stats = service.infinidysk
        if stats is None or not stats.live:
            continue
        for read in [*stats.reads, *stats.recent_reads]:
            keep(index.by_name, read.file_name.lower(), read)
            if read.path:
                keep(index.by_target, _last_segment(read.path), read)
    return index


def match_reads(snap: ServerSnapshot) -> dict[tuple[str, str], ActiveRead]:
    """Pair this server's Emby sessions with the InfiniDysk read of the same file.

    First by the STRM target (InfiniDysk's item id, the last segment of both the .strm URL and
    the read's ``path``), else by file name. Only live feeds count: without one there's no
    read list and no problem reports. An unmatched session gets no InfiniDysk health at all
    rather than a guess.
    """
    index = build_read_index(snap.services)
    if not index.by_name:
        return {}
    out = {}
    for service in snap.services:
        for session in service.sessions:
            read = index.find(session.strm_target, session.file_name)
            if read is not None:
                out[(session.source.key, session.session_id)] = read
    return out
