"""Servers: linked peers (read from) and access tokens (read by)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from mediahub.context import AppContext, get_context
from mediahub.db.models import Peer
from mediahub.db.session import session_scope
from mediahub.logging import get_logger
from mediahub.peers import repository, tokens
from mediahub.peers.client import insecure_transport
from mediahub.peers.forms import PeerForm, parse_grant_label, parse_peer_form
from mediahub.security.csrf import verify_csrf
from mediahub.web.deps import require_auth
from mediahub.web.templating import redirect, render

log = get_logger(__name__)
router = APIRouter(
    prefix="/servers/peers", dependencies=[Depends(require_auth), Depends(verify_csrf)]
)

DUPLICATE_NAME = "Another linked server already uses this name."


async def _grants_partial(request: Request, ctx: AppContext, **extra: Any) -> HTMLResponse:
    def load() -> dict[str, Any]:
        with session_scope(ctx.sessions) as db:
            return {"grants": tokens.list_grants(db)}

    context = await run_in_threadpool(load)
    return render(request, "partials/settings/grants.html", {**context, **extra})


# ----- access tokens (other servers reading this one) --------------------------------------
@router.post("/grants", response_class=HTMLResponse)
async def create_grant(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    label, error = parse_grant_label(await request.form())
    if error:
        return await _grants_partial(request, ctx, grant_error=error, grant_label=label)

    def create() -> str:
        with session_scope(ctx.sessions) as db:
            _, token = tokens.create_grant(db, label)
            return token

    token = await run_in_threadpool(create)
    log.info("peer_grant_created", label=label)
    return await _grants_partial(request, ctx, new_token=token, new_label=label)


@router.post("/grants/{grant_id}/revoke", response_class=HTMLResponse)
async def revoke_grant(
    grant_id: int, request: Request, ctx: AppContext = Depends(get_context)
) -> HTMLResponse:
    def revoke() -> str | None:
        with session_scope(ctx.sessions) as db:
            grant = tokens.revoke_grant(db, grant_id)
            return grant.label if grant else None

    label = await run_in_threadpool(revoke)
    if label is None:
        raise HTTPException(404, "Token not found")
    log.info("peer_grant_revoked", label=label)
    return await _grants_partial(request, ctx)


@router.post("/grants/{grant_id}/delete", response_class=HTMLResponse)
async def delete_grant(
    grant_id: int, request: Request, ctx: AppContext = Depends(get_context)
) -> HTMLResponse:
    def remove() -> bool:
        with session_scope(ctx.sessions) as db:
            return tokens.delete_grant(db, grant_id)

    if not await run_in_threadpool(remove):
        raise HTTPException(404, "Only revoked tokens can be deleted")
    return await _grants_partial(request, ctx)


# ----- linked servers (this server reading others) -----------------------------------------
def _form_context(form: PeerForm, peer: Peer | None) -> dict[str, Any]:
    return {
        "form": form,
        "peer": peer,
        "token_stored": peer is not None and repository.token_readable(peer),
        "token_unreadable": peer is not None and not repository.token_readable(peer),
        "insecure": bool(form.base_url) and insecure_transport(form.base_url),
        "page_title": f"Edit {peer.name}" if peer else "Link a server",
        "back_href": "/servers",
    }


def _load_peer(ctx: AppContext, peer_id: str) -> Peer:
    with session_scope(ctx.sessions) as db:
        row = repository.get_peer(db, peer_id)
        if row is None:
            raise HTTPException(404, "Linked server not found")
        return row


@router.get("/new", response_class=HTMLResponse)
async def new_peer(request: Request) -> HTMLResponse:
    return render(request, "pages/settings/peer_form.html", _form_context(PeerForm(), None))


@router.post("/new")
async def create_peer(request: Request, ctx: AppContext = Depends(get_context)) -> Response:
    form = parse_peer_form(await request.form())
    if not form.valid:
        return render(
            request, "pages/settings/peer_form.html", _form_context(form, None), status_code=422
        )

    def save() -> str | None:
        with session_scope(ctx.sessions) as db:
            row = Peer(
                name=form.name,
                base_url=form.base_url,
                token={"token": form.new_token},
                enabled=form.enabled,
            )
            db.add(row)
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                return None
            return row.id

    peer_id = await run_in_threadpool(save)
    if peer_id is None:
        form.errors["name"] = DUPLICATE_NAME
        return render(
            request, "pages/settings/peer_form.html", _form_context(form, None), status_code=422
        )
    log.info("peer_created", name=form.name)
    await ctx.monitor.reload_peer(peer_id)
    return redirect("/servers?saved=peer", request)


@router.post("/test", response_class=HTMLResponse)
async def test_peer(request: Request, ctx: AppContext = Depends(get_context)) -> HTMLResponse:
    """Test the values currently in the form (before saving). A blank token uses the stored one."""
    data = await request.form()
    peer_id = data.get("peer_id")
    stored: str | None = None
    if isinstance(peer_id, str) and peer_id:
        row = await run_in_threadpool(_load_peer, ctx, peer_id)
        config = repository.safe_to_config(row)
        stored = config.token if config else None
    form = parse_peer_form(data, has_stored_token=stored is not None)
    form.errors.pop("name", None)  # a name isn't needed to test
    if not form.valid:
        return render(request, "partials/settings/test_result.html", {"form_errors": form.errors})
    result = await ctx.monitor.test_peer(form.to_config("test", stored))
    return render(request, "partials/settings/test_result.html", {"result": result})


@router.get("/{peer_id}", response_class=HTMLResponse)
async def edit_peer(
    peer_id: str, request: Request, ctx: AppContext = Depends(get_context)
) -> HTMLResponse:
    row = await run_in_threadpool(_load_peer, ctx, peer_id)
    form = PeerForm(name=row.name, base_url=row.base_url, enabled=row.enabled)
    return render(request, "pages/settings/peer_form.html", _form_context(form, row))


@router.post("/{peer_id}")
async def update_peer(
    peer_id: str, request: Request, ctx: AppContext = Depends(get_context)
) -> Response:
    row = await run_in_threadpool(_load_peer, ctx, peer_id)
    form = parse_peer_form(await request.form(), has_stored_token=repository.token_readable(row))
    if not form.valid:
        return render(
            request, "pages/settings/peer_form.html", _form_context(form, row), status_code=422
        )

    def save() -> bool:
        with session_scope(ctx.sessions) as db:
            current = repository.get_peer(db, peer_id)
            if current is None:
                raise HTTPException(404, "Linked server not found")
            current.name = form.name
            current.base_url = form.base_url
            current.enabled = form.enabled
            if form.new_token:
                current.token = {"token": form.new_token}
            # Saving accepts whichever MediaHub now answers at this URL.
            current.remote_server_id = None
            try:
                db.flush()
            except IntegrityError:
                db.rollback()
                return False
            return True

    if not await run_in_threadpool(save):
        form.errors["name"] = DUPLICATE_NAME
        return render(
            request, "pages/settings/peer_form.html", _form_context(form, row), status_code=422
        )
    log.info("peer_updated", name=form.name)
    await ctx.monitor.reload_peer(peer_id)
    return redirect("/servers?saved=peer", request)


@router.post("/{peer_id}/toggle")
async def toggle_peer(
    peer_id: str, request: Request, ctx: AppContext = Depends(get_context)
) -> Response:
    def flip() -> None:
        with session_scope(ctx.sessions) as db:
            row = repository.get_peer(db, peer_id)
            if row is None:
                raise HTTPException(404, "Linked server not found")
            row.enabled = not row.enabled

    await run_in_threadpool(flip)
    await ctx.monitor.reload_peer(peer_id)
    return redirect("/servers", request)


@router.post("/{peer_id}/delete")
async def delete_peer(
    peer_id: str, request: Request, ctx: AppContext = Depends(get_context)
) -> Response:
    def remove() -> str:
        with session_scope(ctx.sessions) as db:
            row = repository.get_peer(db, peer_id)
            if row is None:
                raise HTTPException(404, "Linked server not found")
            name = row.name
            db.delete(row)
            return name

    name = await run_in_threadpool(remove)
    log.info("peer_deleted", name=name)
    await ctx.monitor.reload_peer(peer_id)
    return redirect("/servers?saved=peer-deleted", request)
