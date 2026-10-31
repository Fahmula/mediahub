"""Owns the poller and keeps poll targets in sync with configured instances and peers."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.orm import Session, sessionmaker

from mediahub.config import Settings
from mediahub.db.session import session_scope
from mediahub.instances import repository
from mediahub.integrations.base import ConnectionTestResult, InstanceConfig, Integration
from mediahub.integrations.demo import (
    DemoIntegration,
    DemoNextEpisodeTarget,
    DemoPeerTarget,
    demo_local_configs,
)
from mediahub.integrations.registry import integration_class
from mediahub.logging import get_logger
from mediahub.models.common import ServiceStatus, ServiceType, SourceRef
from mediahub.models.snapshot import ServiceSnapshot
from mediahub.monitoring.demand import DemandTracker
from mediahub.monitoring.next_episode import NextEpisodeTarget
from mediahub.monitoring.poller import Poller
from mediahub.monitoring.store import SnapshotStore
from mediahub.monitoring.targets import InstanceTarget
from mediahub.peers import repository as peer_repository
from mediahub.peers.client import PeerClient
from mediahub.peers.repository import PeerConfig
from mediahub.peers.target import PeerTarget, target_key

log = get_logger(__name__)

TEST_TIMEOUT_S = 20.0


@dataclass(frozen=True)
class LoadedInstance:
    id: str
    name: str
    service_type: ServiceType
    order: int
    config: InstanceConfig | None  # None when stored credentials can't be decrypted


@dataclass(frozen=True)
class LoadedPeer:
    id: str
    name: str
    enabled: bool
    config: PeerConfig | None  # None when the stored token can't be decrypted
    last_success_at: datetime | None = None


class MonitoringService:
    def __init__(
        self,
        settings: Settings,
        sessions: sessionmaker[Session],
        store: SnapshotStore,
        demand: DemandTracker,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self.store = store
        self.demand = demand
        self.poller = Poller(demand)
        self._background: set[asyncio.Task[None]] = set()

    # ----- lifecycle -------------------------------------------------------------------
    async def start(self) -> None:
        self.demand.bind_loop(asyncio.get_running_loop())
        items = await asyncio.to_thread(self._load)
        for item in items:
            await self._apply(item)
        for peer in await asyncio.to_thread(self._load_peers):
            await self._apply_peer(peer)
        await self.poller.add(NextEpisodeTarget(self.store, self.local_integrations))
        if self._settings.demo_mode:
            for order, config in enumerate(demo_local_configs(), start=len(items)):
                source = self.source_for(config)
                await self.poller.add(
                    InstanceTarget(DemoIntegration(config, source), self.store, order=order)
                )
            await self.poller.add(DemoPeerTarget(self.store))
            await self.poller.add(DemoNextEpisodeTarget(self.store))
        log.info("monitoring_started", targets=len(self.poller.keys), demo=self._settings.demo_mode)

    async def stop(self) -> None:
        await self.poller.stop()
        await self.store.issue_states.aclose()
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)

    # ----- instances -------------------------------------------------------------------
    def source_for(self, config: InstanceConfig) -> SourceRef:
        identity = self.store.identity
        return SourceRef(
            server_id=identity.server_id,
            server_name=identity.server_name,
            instance_id=config.id,
            instance_name=config.name,
            service_type=config.service_type,
        )

    def _load(self) -> list[LoadedInstance]:
        with session_scope(self._sessions) as db:
            return [
                LoadedInstance(
                    id=row.id,
                    name=row.name,
                    service_type=ServiceType(row.service_type),
                    order=order,
                    config=repository.safe_to_config(row),
                )
                for order, row in enumerate(repository.list_instances(db))
            ]

    async def _apply(self, item: LoadedInstance) -> None:
        source = SourceRef(
            server_id=self.store.identity.server_id,
            server_name=self.store.identity.server_name,
            instance_id=item.id,
            instance_name=item.name,
            service_type=item.service_type,
        )
        config = item.config
        if config is None or not config.enabled:
            await self.poller.remove(item.id)
            snapshot = ServiceSnapshot(source=source, status=ServiceStatus.DISABLED)
            if config is None:
                # Keep the instance visible with a clear error rather than dropping it.
                snapshot = ServiceSnapshot(
                    source=source,
                    status=ServiceStatus.ERROR,
                    error="Stored credentials can't be decrypted (was the secret key changed?). "
                    "Re-enter them under Servers (Edit).",
                )
            self.store.put_local(snapshot, order=item.order)
            return
        integration = integration_class(config.service_type)(config, source)
        await self.poller.add(
            InstanceTarget(
                integration, self.store, order=item.order, on_status=self._persist_status
            )
        )

    def integration(self, instance_id: str) -> Integration | None:
        """The live integration polling a local instance, e.g. to proxy its images."""
        target = self.poller.get(instance_id)
        return target.integration if isinstance(target, InstanceTarget) else None

    def local_integrations(self) -> list[Integration]:
        """Every live integration of an enabled local instance."""
        targets = (self.poller.get(key) for key in self.poller.keys)
        return [t.integration for t in targets if isinstance(t, InstanceTarget)]

    async def reload_instance(self, instance_id: str) -> None:
        """Re-read one instance after it was created, edited, enabled/disabled or deleted."""
        items = await asyncio.to_thread(self._load)
        item = next((i for i in items if i.id == instance_id), None)
        if item is None:
            await self.poller.remove(instance_id)
            self.store.remove_local(instance_id)
            return
        await self._apply(item)
        if item.config is not None and item.config.enabled:
            await self.poller.poll_now(instance_id)

    async def refresh_identity(self) -> None:
        """After a server rename, rebuild targets so new snapshots carry the new name."""
        for item in await asyncio.to_thread(self._load):
            await self._apply(item)

    async def test(self, config: InstanceConfig) -> ConnectionTestResult:
        integration = integration_class(config.service_type)(config, self.source_for(config))
        try:
            return await asyncio.wait_for(integration.test_connection(), TEST_TIMEOUT_S)
        except TimeoutError:
            result = ConnectionTestResult(ok=False)
            result.add("Connection test", False, f"Timed out after {TEST_TIMEOUT_S:.0f}s")
            return result
        finally:
            await integration.aclose()

    # ----- peers -----------------------------------------------------------------------
    def _load_peers(self) -> list[LoadedPeer]:
        with session_scope(self._sessions) as db:
            return [
                LoadedPeer(
                    row.id,
                    row.name,
                    row.enabled,
                    peer_repository.safe_to_config(row),
                    row.last_success_at,
                )
                for row in peer_repository.list_peers(db)
            ]

    async def _apply_peer(self, peer: LoadedPeer) -> None:
        if not peer.enabled:
            await self.poller.remove(target_key(peer.id))
            self.store.remove_peer(peer.id, forget=False)
            return
        state = self.store.peer(peer.id, peer.name)
        state.last_success_at = state.last_success_at or peer.last_success_at
        if peer.config is None:
            await self.poller.remove(target_key(peer.id))
            state.status = ServiceStatus.ERROR
            state.error = (
                "The stored access token can't be decrypted (was the secret key changed?). "
                "Enter it again under Servers (Edit)."
            )
            return
        await self.poller.add(
            PeerTarget(
                peer.config,
                self.store,
                self.demand,
                on_status=self._persist_peer_status,
                on_server_id=self._remember_server_id,
            )
        )

    def peer_client(self, peer_id: str) -> PeerClient | None:
        target = self.poller.get(target_key(peer_id))
        return target.client if isinstance(target, PeerTarget) else None

    async def reload_peer(self, peer_id: str) -> None:
        """Re-read one peer after it was created, edited, enabled/disabled or deleted."""
        peers = await asyncio.to_thread(self._load_peers)
        peer = next((p for p in peers if p.id == peer_id), None)
        if peer is None:
            await self.poller.remove(target_key(peer_id))
            self.store.remove_peer(peer_id)
            return
        if peer.config is not None and self.store.get_peer(peer_id) is not None:
            # Name, URL or token changed: start from a clean slate, not the old data.
            self.store.remove_peer(peer_id, forget=False)
        await self._apply_peer(peer)
        if peer.enabled and peer.config is not None:
            await self.poller.poll_now(target_key(peer_id))

    async def test_peer(self, config: PeerConfig) -> ConnectionTestResult:
        client = PeerClient(config, self.store.identity.server_id)
        try:
            return await asyncio.wait_for(client.test(), TEST_TIMEOUT_S)
        except TimeoutError:
            result = ConnectionTestResult(ok=False)
            result.add("Reach the peer", False, f"Timed out after {TEST_TIMEOUT_S:.0f}s")
            return result
        finally:
            await client.aclose()

    # ----- status persistence ----------------------------------------------------------
    def _persist_status(self, instance_id: str, ok: bool, error: str | None, at: datetime) -> None:
        def write() -> None:
            with session_scope(self._sessions) as db:
                repository.record_status(db, instance_id, ok, error, at)

        self._in_background(write)

    def _persist_peer_status(self, peer_id: str, ok: bool, error: str | None, at: datetime) -> None:
        def write() -> None:
            with session_scope(self._sessions) as db:
                peer_repository.record_status(db, peer_id, ok, error, at)

        self._in_background(write)

    def _remember_server_id(self, peer_id: str, server_id: str) -> None:
        def write() -> None:
            with session_scope(self._sessions) as db:
                peer_repository.remember_server_id(db, peer_id, server_id)

        self._in_background(write)

    def _in_background(self, write: Callable[[], None]) -> None:
        task = asyncio.create_task(asyncio.to_thread(write))
        self._background.add(task)
        task.add_done_callback(self._background.discard)
