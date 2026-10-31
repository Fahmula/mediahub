"""Persistence for service instances."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from mediahub.db.crypto import DecryptionError
from mediahub.db.models import ServiceInstance
from mediahub.db.types import UNDECRYPTABLE
from mediahub.integrations.base import InstanceConfig
from mediahub.models.common import ServiceType


@dataclass(frozen=True)
class ManagedInstance:
    """What the Servers page needs of a row: no secrets, so nothing is decrypted to poll it."""

    id: str
    name: str
    service_type: ServiceType
    enabled: bool


def list_instances(db: Session) -> list[ServiceInstance]:
    return list(
        db.scalars(
            select(ServiceInstance).order_by(
                ServiceInstance.service_type, ServiceInstance.sort_order, ServiceInstance.name
            )
        )
    )


def list_managed(db: Session) -> list[ManagedInstance]:
    rows = db.execute(
        select(
            ServiceInstance.id,
            ServiceInstance.name,
            ServiceInstance.service_type,
            ServiceInstance.enabled,
        ).order_by(ServiceInstance.service_type, ServiceInstance.sort_order, ServiceInstance.name)
    )
    return [ManagedInstance(r.id, r.name, ServiceType(r.service_type), r.enabled) for r in rows]


def get_instance(db: Session, instance_id: str) -> ServiceInstance | None:
    return db.get(ServiceInstance, instance_id)


def to_config(row: ServiceInstance) -> InstanceConfig:
    """Decrypt a row into an integration config. Raises ``DecryptionError`` on a bad key."""
    if not secrets_readable(row):
        raise DecryptionError(f"credentials for {row.name} cannot be decrypted")
    return InstanceConfig(
        id=row.id,
        service_type=ServiceType(row.service_type),
        name=row.name,
        base_url=row.base_url,
        secrets=dict(row.secrets or {}),
        options=dict(row.options or {}),
        enabled=row.enabled,
        poll_interval_s=row.poll_interval_s,
    )


def secrets_readable(row: ServiceInstance) -> bool:
    return not (row.secrets or {}).get(UNDECRYPTABLE)


def safe_to_config(row: ServiceInstance) -> InstanceConfig | None:
    try:
        return to_config(row)
    except DecryptionError:
        return None


def record_status(db: Session, instance_id: str, ok: bool, error: str | None, at: datetime) -> None:
    row = db.get(ServiceInstance, instance_id)
    if row is None:
        return
    if ok:
        row.last_success_at = at
        row.last_error = None
    else:
        row.last_error = error
        row.last_error_at = at
