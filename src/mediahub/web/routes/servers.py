"""Servers: status of this server and linked ones, and managing their services."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from mediahub.aggregation.dashboard import SCOPE_ALL, build_view
from mediahub.context import AppContext, get_context
from mediahub.db.models import ServiceInstance
from mediahub.db.session import session_scope
from mediahub.instances import repository
from mediahub.instances.forms import InstanceForm, parse_instance_form
from mediahub.instances.repository import ManagedInstance
from mediahub.integrations.registry import integration_class
from mediahub.logging import get_logger
from mediahub.models.common import ServiceType
from mediahub.peers import repository as peer_repository
from mediahub.peers import tokens
from mediahub.peers.repository import ManagedPeer
from mediahub.security.csrf import verify_csrf
from mediahub.web.deps import require_auth
from mediahub.web.templating import redirect, render

log = get_logger(__name__)
router = APIRouter(dependencies=[Depends(require_auth), Depends(verify_csrf)])


# ----- status ------------------------------------------------------------------------------
def _load_managed(ctx: AppContext) -> tuple[list[ManagedInstance], list[ManagedPeer]]:
    with session_scope(ctx.sessions) as db:
        return repository.list_managed(db), peer_repository.list_managed(db)


def _servers_context(
    ctx: AppContext, instances: list[ManagedInstance], peers: list[ManagedPeer]
) -> dict[str, Any]:
    """Cards come from the store; DB rows add what can be managed and what it doesn't list.

    Demo services and peers have no rows, so they get no menus. Disabled peers and instances
    that haven't been polled yet are only in the database.
    """
    view = build_view(ctx.monitor.store, SCOPE_ALL)
    polled = {s.source.instance_id for card in view.servers for s in card.services}
    cards = {card.key for card in view.servers}
    return {
        "view": view,
        "instances": {i.id: i for i in instances},
        "peers": {p.id: p for p in peers},
        "unpolled_instances": [i for i in instances if i.id not in polled],
        "unlisted_peers": [p for p in peers if p.id not in cards],
    }


@router.get("/servers", response_class=HTMLResponse)
async def servers(
    request: Request, ctx: AppContext = Depends(get_context), saved: str | None = None
) -> HTMLResponse:
    ctx.monitor.demand.touch()

    def load() -> tuple[list[ManagedInstance], list[ManagedPeer], list[Any]]:
        with session_scope(ctx.sessions) as db:
            return (
                repository.list_managed(db),
                peer_repository.list_managed(db),
                tokens.list_grants(db),
            )

    instances, peers, grants = await run_in_threadpool(load)
    return render(
        request,
        "pages/servers.html",
        {
            **_servers_context(ctx, instances, peers),
            "grants": grants,
            "notice": saved,
            "page_title": "Servers",
        },
    )


@router.get("/partials/servers", response_class=HTMLResponse)
async def servers_partial(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    ctx.monitor.demand.touch()
    instances, peers = await run_in_threadpool(_load_managed, ctx)
    return render(request, "partials/servers.html", _servers_context(ctx, instances, peers))


# ----- services ----------------------------------------------------------------------------
def _type_or_404(value: str | None) -> ServiceType:
    try:
        return ServiceType(value or "")
    except ValueError as exc:
        raise HTTPException(404, "Unknown service type") from exc


def _form_context(form: InstanceForm, instance: ServiceInstance | None) -> dict[str, Any]:
    integration = integration_class(form.service_type)
    stored = (
        set()
        if instance is None
        else {k for k, v in (instance.secrets or {}).items() if v and not k.startswith("__")}
    )
    return {
        "form": form,
        "instance": instance,
        "integration": integration,
        "stored_secrets": stored,
        "secrets_unreadable": instance is not None and not repository.secrets_readable(instance),
        "page_title": f"Edit {instance.name}" if instance else f"Add {form.service_type.label}",
        "back_href": "/servers",
    }


@router.get("/servers/services/new", response_class=HTMLResponse)
async def new_service(request: Request, type: str | None = None) -> HTMLResponse:
    if not type:
        return render(
            request,
            "pages/settings/service_type.html",
            {"page_title": "Add a service", "back_href": "/servers", "types": list(ServiceType)},
        )
    service_type = _type_or_404(type)
    integration = integration_class(service_type)
    form = InstanceForm(service_type=service_type, base_url=integration.default_url)
    return render(request, "pages/settings/service_form.html", _form_context(form, None))


@router.post("/servers/services/new")
async def create_service(request: Request, ctx: AppContext = Depends(get_context)) -> Response:
    data = await request.form()
    try:
        form = parse_instance_form(data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not form.valid:
        return render(
            request, "pages/settings/service_form.html", _form_context(form, None), status_code=422
        )

    def save() -> str | None:
        with session_scope(ctx.sessions) as db:
            row = ServiceInstance(
                service_type=form.service_type.value,
                name=form.name,
                base_url=form.base_url,
                secrets=form.merged_secrets(None),
                options={**form.options, "verify_tls": form.verify_tls},
                enabled=form.enabled,
                poll_interval_s=form.poll_interval_s,
            )
            db.add(row)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                return None
            return row.id

    instance_id = await run_in_threadpool(save)
    if instance_id is None:
        form.errors["name"] = f"Another {form.service_type.label} instance already uses this name."
        return render(
            request, "pages/settings/service_form.html", _form_context(form, None), status_code=422
        )
    log.info("instance_created", type=form.service_type.value, name=form.name)
    await ctx.monitor.reload_instance(instance_id)
    return redirect("/servers?saved=service", request)


def _load_instance(ctx: AppContext, instance_id: str) -> ServiceInstance:
    with session_scope(ctx.sessions) as db:
        row = repository.get_instance(db, instance_id)
        if row is None:
            raise HTTPException(404, "Service not found")
        return row


@router.post("/servers/services/test", response_class=HTMLResponse)
async def test_service(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    """Test the values currently in the form (before saving). Blank secrets use stored ones."""
    data = dict(await request.form())
    instance_id = data.get("instance_id")
    stored: dict[str, str] = {}
    if isinstance(instance_id, str) and instance_id:
        row = await run_in_threadpool(_load_instance, ctx, instance_id)
        data["service_type"] = row.service_type
        stored = row.secrets if repository.secrets_readable(row) else {}
    try:
        form = parse_instance_form(data, stored_secrets=stored)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not form.valid:
        return render(request, "partials/settings/test_result.html", {"form_errors": form.errors})
    config = form.to_config(instance_id if isinstance(instance_id, str) else "test", stored)
    result = await ctx.monitor.test(replace(config, name=form.name or "Test"))
    return render(request, "partials/settings/test_result.html", {"result": result})


@router.get("/servers/services/{instance_id}", response_class=HTMLResponse)
async def edit_service(
    instance_id: str, request: Request, ctx: AppContext = Depends(get_context)
) -> HTMLResponse:
    row = await run_in_threadpool(_load_instance, ctx, instance_id)
    form = InstanceForm(
        service_type=ServiceType(row.service_type),
        name=row.name,
        base_url=row.base_url,
        enabled=row.enabled,
        verify_tls=bool((row.options or {}).get("verify_tls", True)),
        poll_interval_s=row.poll_interval_s,
        options={k: v for k, v in (row.options or {}).items() if k != "verify_tls"},
    )
    return render(request, "pages/settings/service_form.html", _form_context(form, row))


@router.post("/servers/services/{instance_id}")
async def update_service(
    instance_id: str, request: Request, ctx: AppContext = Depends(get_context)
) -> Response:
    row = await run_in_threadpool(_load_instance, ctx, instance_id)
    data = dict(await request.form())
    data["service_type"] = row.service_type  # the type of an existing instance can't change
    stored = row.secrets if repository.secrets_readable(row) else {}
    form = parse_instance_form(data, stored_secrets=stored)
    if not form.valid:
        return render(
            request, "pages/settings/service_form.html", _form_context(form, row), status_code=422
        )

    def save() -> bool:
        with session_scope(ctx.sessions) as db:
            current = repository.get_instance(db, instance_id)
            if current is None:
                raise HTTPException(404, "Service not found")
            current.name = form.name
            current.base_url = form.base_url
            current.secrets = form.merged_secrets(stored)
            current.options = {**form.options, "verify_tls": form.verify_tls}
            current.enabled = form.enabled
            current.poll_interval_s = form.poll_interval_s
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                return False
            return True

    if not await run_in_threadpool(save):
        form.errors["name"] = f"Another {form.service_type.label} instance already uses this name."
        return render(
            request, "pages/settings/service_form.html", _form_context(form, row), status_code=422
        )
    log.info("instance_updated", type=form.service_type.value, name=form.name)
    await ctx.monitor.reload_instance(instance_id)
    return redirect("/servers?saved=service", request)


@router.post("/servers/services/{instance_id}/toggle")
async def toggle_service(
    instance_id: str, request: Request, ctx: AppContext = Depends(get_context)
) -> Response:
    def flip() -> None:
        with session_scope(ctx.sessions) as db:
            row = repository.get_instance(db, instance_id)
            if row is None:
                raise HTTPException(404, "Service not found")
            row.enabled = not row.enabled

    await run_in_threadpool(flip)
    await ctx.monitor.reload_instance(instance_id)
    return redirect("/servers", request)


@router.post("/servers/services/{instance_id}/delete")
async def delete_service(
    instance_id: str, request: Request, ctx: AppContext = Depends(get_context)
) -> Response:
    def remove() -> str:
        with session_scope(ctx.sessions) as db:
            row = repository.get_instance(db, instance_id)
            if row is None:
                raise HTTPException(404, "Service not found")
            name = row.name
            db.delete(row)
            return name

    name = await run_in_threadpool(remove)
    log.info("instance_deleted", name=name)
    await ctx.monitor.reload_instance(instance_id)
    return redirect("/servers?saved=deleted", request)
