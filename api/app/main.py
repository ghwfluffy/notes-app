from __future__ import annotations

import base64
import hashlib
import html as html_module
import json
import mimetypes
import secrets
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import urlencode

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent_tokens import AgentTokenClaims, require_agent_scope
from app.config import Settings, get_settings
from app.db import get_db
from app.models import AuditEvent, NoteItem, NoteList, NotesUser
from app.schemas import ItemCreate, ItemPatch, ListCreate, ListOrderUpdate, ListPatch


ROOT_DIR = Path(__file__).resolve().parents[2]
WEB_DIR = ROOT_DIR / "web"
BANNER_SCRIPT_PATH = ROOT_DIR / "vendor" / "federated-banner" / "dist" / "browser" / "federated-banner.iife.js"
STARTER_LISTS = (
    ("Movies to Watch", "Films and shows I want to remember.", "#6750a4"),
    ("Games", "Games to play, revisit, or recommend.", "#006c4c"),
    ("Project Ideas", "Things I might want to build or explore.", "#9a4522"),
    ("Date Ideas", "Places and activities for a good date.", "#a73563"),
    ("Quotes", "Lines worth keeping.", "#38608f"),
)

app = FastAPI(title="My Notes", version="0.1.0")


@app.middleware("http")
async def prevent_private_caching(request: Request, call_next):  # type: ignore[no-untyped-def]
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith(("/api/", "/auth/")):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.middleware("http")
async def accept_configured_base_path(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Accept both proxy-stripped and direct requests beneath APP_BASE_PATH."""
    base = get_settings().normalized_app_base_path
    path = request.scope.get("path", "")
    if base and isinstance(path, str) and (path == base or path.startswith(f"{base}/")):
        stripped = path[len(base) :] or "/"
        request.scope["path"] = stripped
        request.scope["raw_path"] = stripped.encode("utf-8")
    return await call_next(request)


def serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_key, salt="my-notes-session")


def b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def pkce_challenge(verifier: str) -> str:
    return b64url(hashlib.sha256(verifier.encode("ascii")).digest())


def app_url(settings: Settings, path: str = "") -> str:
    suffix = path if not path or path.startswith("/") else f"/{path}"
    return f"{settings.normalized_app_base_path}{suffix}" or "/"


def safe_next(settings: Settings, next_path: str | None) -> str:
    if not next_path or next_path.startswith("//"):
        return app_url(settings, "/")
    base = settings.normalized_app_base_path
    if base and (next_path == base or next_path.startswith(f"{base}/")):
        return next_path
    if not base and next_path.startswith("/"):
        return next_path
    return app_url(settings, "/")


def read_signed_cookie(value: str | None, settings: Settings, max_age: int) -> dict[str, object] | None:
    if not value:
        return None
    try:
        payload = serializer(settings).loads(value, max_age=max_age)
    except BadSignature:
        return None
    return payload if isinstance(payload, dict) else None


def current_user(request: Request, settings: Settings) -> dict[str, object] | None:
    payload = read_signed_cookie(
        request.cookies.get(settings.session_cookie_name),
        settings,
        settings.session_duration_minutes * 60,
    )
    return payload if payload and isinstance(payload.get("sub"), str) and payload.get("sub") else None


def require_user(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    user = current_user(request, settings)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required.")
    return user


def user_subject(user: dict[str, object]) -> str:
    return str(user["sub"])


def serialize_item(item: NoteItem) -> dict[str, object]:
    return {
        "id": item.id,
        "list_id": item.list_id,
        "title": item.title,
        "details": item.details,
        "completed": item.completed,
        "position": item.position,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


def serialize_list(note_list: NoteList, *, include_items: bool = True) -> dict[str, object]:
    items = [serialize_item(item) for item in note_list.items] if include_items else []
    return {
        "id": note_list.id,
        "name": note_list.name,
        "description": note_list.description,
        "color": note_list.color,
        "position": note_list.position,
        "item_count": len(note_list.items),
        "active_item_count": sum(1 for item in note_list.items if not item.completed),
        "items": items,
        "created_at": note_list.created_at,
        "updated_at": note_list.updated_at,
    }


def audit(
    db: Session,
    *,
    owner: str,
    actor: str,
    action: str,
    entity_type: str,
    entity_id: str,
    details: dict[str, object] | None = None,
) -> None:
    db.add(
        AuditEvent(
            owner_subject=owner,
            actor_type=actor,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details or {},
        )
    )


def ensure_starter_lists(db: Session, owner: str) -> None:
    if db.get(NotesUser, owner) is not None:
        return
    db.add(NotesUser(owner_subject=owner))
    try:
        db.flush()
    except IntegrityError:
        # A simultaneous first browser/agent request initialized this owner.
        db.rollback()
        return
    for position, (name, description, color) in enumerate(STARTER_LISTS):
        note_list = NoteList(
            owner_subject=owner,
            name=name,
            description=description,
            color=color,
            position=position,
        )
        db.add(note_list)
        db.flush()
        audit(
            db,
            owner=owner,
            actor="system",
            action="list.seeded",
            entity_type="list",
            entity_id=note_list.id,
            details={"name": name},
        )
    db.commit()


def all_lists(db: Session, owner: str) -> list[NoteList]:
    ensure_starter_lists(db, owner)
    return list(
        db.scalars(
            select(NoteList)
            .where(NoteList.owner_subject == owner)
            .order_by(NoteList.position, NoteList.created_at)
        ).unique()
    )


def owned_list(db: Session, owner: str, list_id: str) -> NoteList:
    note_list = db.scalar(
        select(NoteList).where(NoteList.id == list_id, NoteList.owner_subject == owner)
    )
    if note_list is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="List not found.")
    return note_list


def owned_item(db: Session, owner: str, item_id: str) -> NoteItem:
    item = db.scalar(
        select(NoteItem).where(NoteItem.id == item_id, NoteItem.owner_subject == owner)
    )
    if item is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found.")
    return item


def create_list_record(db: Session, owner: str, payload: ListCreate, actor: str) -> NoteList:
    ensure_starter_lists(db, owner)
    maximum_position = db.scalar(
        select(func.max(NoteList.position)).where(NoteList.owner_subject == owner)
    )
    next_position = 0 if maximum_position is None else maximum_position + 1
    note_list = NoteList(
        owner_subject=owner,
        name=payload.name,
        description=payload.description or None,
        color=payload.color.lower(),
        position=next_position,
    )
    db.add(note_list)
    db.flush()
    audit(
        db,
        owner=owner,
        actor=actor,
        action="list.created",
        entity_type="list",
        entity_id=note_list.id,
        details={"name": note_list.name},
    )
    db.commit()
    db.refresh(note_list)
    return note_list


def update_list_record(
    db: Session, owner: str, list_id: str, payload: ListPatch, actor: str
) -> NoteList:
    note_list = owned_list(db, owner, list_id)
    changed: list[str] = []
    for field in payload.model_fields_set:
        value = getattr(payload, field)
        if field == "position" and isinstance(value, int):
            current_order = list(
                db.scalars(
                    select(NoteList)
                    .where(NoteList.owner_subject == owner)
                    .order_by(NoteList.position, NoteList.created_at)
                    .with_for_update()
                ).unique()
            )
            if value >= len(current_order):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="List position is outside the current list order.",
                )
            current_order.remove(note_list)
            current_order.insert(value, note_list)
            for position, ordered_list in enumerate(current_order):
                ordered_list.position = position
            changed.append(field)
            continue
        if field == "color" and isinstance(value, str):
            value = value.lower()
        if field == "description" and value == "":
            value = None
        setattr(note_list, field, value)
        changed.append(field)
    audit(
        db,
        owner=owner,
        actor=actor,
        action="list.updated",
        entity_type="list",
        entity_id=note_list.id,
        details={"fields": changed},
    )
    db.commit()
    db.refresh(note_list)
    return note_list


def reorder_list_records(
    db: Session, owner: str, payload: ListOrderUpdate, actor: str
) -> list[NoteList]:
    ensure_starter_lists(db, owner)
    current_order = list(
        db.scalars(
            select(NoteList)
            .where(NoteList.owner_subject == owner)
            .order_by(NoteList.position, NoteList.created_at)
            .with_for_update()
        ).unique()
    )
    current_by_id = {note_list.id: note_list for note_list in current_order}
    if len(payload.list_ids) != len(current_order) or set(payload.list_ids) != set(current_by_id):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The list collection changed. Refresh it before saving a new order.",
        )

    ordered_lists = [current_by_id[list_id] for list_id in payload.list_ids]
    changed_count = 0
    for position, note_list in enumerate(ordered_lists):
        if note_list.position != position:
            note_list.position = position
            changed_count += 1

    if changed_count:
        audit(
            db,
            owner=owner,
            actor=actor,
            action="lists.reordered",
            entity_type="list_order",
            entity_id=ordered_lists[0].id,
            details={"list_ids": payload.list_ids, "changed_count": changed_count},
        )
    db.commit()
    return ordered_lists


def delete_list_record(db: Session, owner: str, list_id: str, actor: str) -> dict[str, object]:
    note_list = owned_list(db, owner, list_id)
    name = note_list.name
    item_count = len(note_list.items)
    db.delete(note_list)
    audit(
        db,
        owner=owner,
        actor=actor,
        action="list.deleted",
        entity_type="list",
        entity_id=list_id,
        details={"name": name, "item_count": item_count},
    )
    db.commit()
    return {"deleted": True, "id": list_id, "name": name}


def create_item_record(
    db: Session, owner: str, list_id: str, payload: ItemCreate, actor: str
) -> NoteItem:
    note_list = owned_list(db, owner, list_id)
    maximum_position = db.scalar(
        select(func.max(NoteItem.position)).where(NoteItem.list_id == list_id)
    )
    next_position = 0 if maximum_position is None else maximum_position + 1
    item = NoteItem(
        list_id=note_list.id,
        owner_subject=owner,
        title=payload.title,
        details=payload.details or None,
        completed=payload.completed,
        position=next_position,
    )
    db.add(item)
    db.flush()
    audit(
        db,
        owner=owner,
        actor=actor,
        action="item.created",
        entity_type="item",
        entity_id=item.id,
        details={"list_id": list_id, "title": item.title},
    )
    db.commit()
    db.refresh(item)
    return item


def update_item_record(
    db: Session, owner: str, item_id: str, payload: ItemPatch, actor: str
) -> NoteItem:
    item = owned_item(db, owner, item_id)
    changed: list[str] = []
    for field in payload.model_fields_set:
        value = getattr(payload, field)
        if field == "details" and value == "":
            value = None
        setattr(item, field, value)
        changed.append(field)
    audit(
        db,
        owner=owner,
        actor=actor,
        action="item.updated",
        entity_type="item",
        entity_id=item.id,
        details={"fields": changed, "list_id": item.list_id},
    )
    db.commit()
    db.refresh(item)
    return item


def delete_item_record(db: Session, owner: str, item_id: str, actor: str) -> dict[str, object]:
    item = owned_item(db, owner, item_id)
    title = item.title
    list_id = item.list_id
    db.delete(item)
    audit(
        db,
        owner=owner,
        actor=actor,
        action="item.deleted",
        entity_type="item",
        entity_id=item_id,
        details={"title": title, "list_id": list_id},
    )
    db.commit()
    return {"deleted": True, "id": item_id, "title": title}


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/auth/oauth/login")
def oauth_login(
    settings: Annotated[Settings, Depends(get_settings)],
    next: str | None = None,
) -> RedirectResponse:
    state = secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    state_payload = {
        "state": state,
        "verifier": verifier,
        "next": safe_next(settings, next),
        "created_at": datetime.now(tz=UTC).isoformat(),
    }
    params = {
        "response_type": "code",
        "client_id": settings.oauth_client_id,
        "redirect_uri": settings.oauth_redirect_uri,
        "scope": settings.oauth_scope,
        "state": state,
        "code_challenge": pkce_challenge(verifier),
        "code_challenge_method": "S256",
    }
    response = RedirectResponse(
        f"{settings.normalized_auth_base_url}/oauth/authorize?{urlencode(params)}",
        status_code=302,
    )
    response.set_cookie(
        settings.oauth_state_cookie_name,
        serializer(settings).dumps(state_payload),
        max_age=600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path=settings.normalized_app_base_path or "/",
    )
    return response


@app.get("/api/v1/auth/oauth/callback")
async def oauth_callback(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    code: str | None = None,
    state: str | None = None,
) -> RedirectResponse:
    state_payload = read_signed_cookie(
        request.cookies.get(settings.oauth_state_cookie_name), settings, 600
    )
    if not code or not state or not state_payload or state_payload.get("state") != state:
        return RedirectResponse(app_url(settings, "/?oauth_error=oauth_state"), status_code=302)
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            token_response = await client.post(
                f"{settings.normalized_oauth_server_base_url}/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": settings.oauth_client_id,
                    "code": code,
                    "redirect_uri": settings.oauth_redirect_uri,
                    "code_verifier": state_payload["verifier"],
                },
            )
            token_response.raise_for_status()
            access_token = token_response.json()["access_token"]
            userinfo_response = await client.get(
                f"{settings.normalized_oauth_server_base_url}/oauth/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            userinfo_response.raise_for_status()
            userinfo = userinfo_response.json()
            if not isinstance(userinfo.get("sub"), str) or not userinfo["sub"]:
                raise ValueError("OAuth userinfo did not contain a subject.")
    except (httpx.HTTPError, KeyError, TypeError, ValueError):
        return RedirectResponse(app_url(settings, "/?oauth_error=oauth_failed"), status_code=302)

    response = RedirectResponse(safe_next(settings, str(state_payload.get("next") or "")), status_code=302)
    response.delete_cookie(
        settings.oauth_state_cookie_name, path=settings.normalized_app_base_path or "/"
    )
    response.set_cookie(
        settings.session_cookie_name,
        serializer(settings).dumps(
            {
                "sub": userinfo["sub"],
                "preferred_username": str(userinfo.get("preferred_username") or ""),
                "name": str(userinfo.get("name") or ""),
                "email": str(userinfo.get("email") or ""),
            }
        ),
        max_age=settings.session_duration_minutes * 60,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path=settings.normalized_app_base_path or "/",
    )
    return response


@app.get("/api/v1/auth/logout")
def logout(settings: Annotated[Settings, Depends(get_settings)]) -> RedirectResponse:
    response = RedirectResponse(app_url(settings, "/"), status_code=302)
    response.delete_cookie(settings.session_cookie_name, path=settings.normalized_app_base_path or "/")
    return response


@app.get("/api/v1/auth/me")
def auth_me(
    user: Annotated[dict[str, object], Depends(require_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, object]:
    return {
        "user": {
            "subject": user_subject(user),
            "displayName": str(user.get("name") or user.get("preferred_username") or "Account"),
            "username": str(user.get("preferred_username") or ""),
            "email": str(user.get("email") or ""),
        },
        "federatedApps": settings.federated_banner_sites,
        "accountSettingsUrl": settings.account_settings_url,
        "appBasePath": settings.normalized_app_base_path,
    }


@app.get("/api/v1/lists")
def browser_list_lists(
    user: Annotated[dict[str, object], Depends(require_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    lists = all_lists(db, user_subject(user))
    return {"lists": [serialize_list(note_list) for note_list in lists]}


@app.post("/api/v1/lists", status_code=201)
def browser_create_list(
    payload: ListCreate,
    user: Annotated[dict[str, object], Depends(require_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    return serialize_list(create_list_record(db, user_subject(user), payload, "user"))


@app.put("/api/v1/lists/order")
def browser_reorder_lists(
    payload: ListOrderUpdate,
    user: Annotated[dict[str, object], Depends(require_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    lists = reorder_list_records(db, user_subject(user), payload, "user")
    return {"lists": [serialize_list(note_list) for note_list in lists]}


@app.patch("/api/v1/lists/{list_id}")
def browser_update_list(
    list_id: str,
    payload: ListPatch,
    user: Annotated[dict[str, object], Depends(require_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    return serialize_list(update_list_record(db, user_subject(user), list_id, payload, "user"))


@app.delete("/api/v1/lists/{list_id}")
def browser_delete_list(
    list_id: str,
    user: Annotated[dict[str, object], Depends(require_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    return delete_list_record(db, user_subject(user), list_id, "user")


@app.post("/api/v1/lists/{list_id}/items", status_code=201)
def browser_create_item(
    list_id: str,
    payload: ItemCreate,
    user: Annotated[dict[str, object], Depends(require_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    return serialize_item(create_item_record(db, user_subject(user), list_id, payload, "user"))


@app.patch("/api/v1/items/{item_id}")
def browser_update_item(
    item_id: str,
    payload: ItemPatch,
    user: Annotated[dict[str, object], Depends(require_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    return serialize_item(update_item_record(db, user_subject(user), item_id, payload, "user"))


@app.delete("/api/v1/items/{item_id}")
def browser_delete_item(
    item_id: str,
    user: Annotated[dict[str, object], Depends(require_user)],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    return delete_item_record(db, user_subject(user), item_id, "user")


@app.get("/api/v1/search")
def browser_search(
    user: Annotated[dict[str, object], Depends(require_user)],
    db: Annotated[Session, Depends(get_db)],
    q: Annotated[str, Query(min_length=1, max_length=200)],
) -> dict[str, object]:
    owner = user_subject(user)
    pattern = f"%{q.strip()}%"
    rows = db.scalars(
        select(NoteItem)
        .where(
            NoteItem.owner_subject == owner,
            or_(NoteItem.title.ilike(pattern), NoteItem.details.ilike(pattern)),
        )
        .order_by(NoteItem.completed, NoteItem.updated_at.desc())
        .limit(100)
    )
    items = []
    for item in rows:
        value = serialize_item(item)
        value["list_name"] = item.note_list.name
        items.append(value)
    return {"items": items, "query": q.strip()}


@app.get("/api/agent/v1/lists")
def agent_list_lists(
    claims: Annotated[AgentTokenClaims, Depends(require_agent_scope("notes.list_lists"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    return {"lists": [serialize_list(note_list, include_items=False) for note_list in all_lists(db, claims.subject)]}


@app.post("/api/agent/v1/lists", status_code=201)
def agent_create_list(
    payload: ListCreate,
    claims: Annotated[AgentTokenClaims, Depends(require_agent_scope("notes.create_list"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    return serialize_list(create_list_record(db, claims.subject, payload, "agent"))


@app.put("/api/agent/v1/lists/order")
def agent_reorder_lists(
    payload: ListOrderUpdate,
    claims: Annotated[AgentTokenClaims, Depends(require_agent_scope("notes.reorder_lists"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    lists = reorder_list_records(db, claims.subject, payload, "agent")
    return {"lists": [serialize_list(note_list, include_items=False) for note_list in lists]}


@app.patch("/api/agent/v1/lists/{list_id}")
def agent_update_list(
    list_id: str,
    payload: ListPatch,
    claims: Annotated[AgentTokenClaims, Depends(require_agent_scope("notes.update_list"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    return serialize_list(update_list_record(db, claims.subject, list_id, payload, "agent"))


@app.delete("/api/agent/v1/lists/{list_id}")
def agent_delete_list(
    list_id: str,
    claims: Annotated[AgentTokenClaims, Depends(require_agent_scope("notes.delete_list"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    return delete_list_record(db, claims.subject, list_id, "agent")


@app.get("/api/agent/v1/lists/{list_id}/items")
def agent_list_items(
    list_id: str,
    claims: Annotated[AgentTokenClaims, Depends(require_agent_scope("notes.list_items"))],
    db: Annotated[Session, Depends(get_db)],
    include_completed: bool = True,
    q: str | None = Query(default=None, max_length=200),
) -> dict[str, object]:
    note_list = owned_list(db, claims.subject, list_id)
    items = note_list.items
    if not include_completed:
        items = [item for item in items if not item.completed]
    if q and q.strip():
        needle = q.casefold().strip()
        items = [
            item
            for item in items
            if needle in item.title.casefold() or needle in (item.details or "").casefold()
        ]
    return {"list": serialize_list(note_list, include_items=False), "items": [serialize_item(item) for item in items]}


@app.post("/api/agent/v1/lists/{list_id}/items", status_code=201)
def agent_create_item(
    list_id: str,
    payload: ItemCreate,
    claims: Annotated[AgentTokenClaims, Depends(require_agent_scope("notes.create_item"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    return serialize_item(create_item_record(db, claims.subject, list_id, payload, "agent"))


@app.patch("/api/agent/v1/items/{item_id}")
def agent_update_item(
    item_id: str,
    payload: ItemPatch,
    claims: Annotated[AgentTokenClaims, Depends(require_agent_scope("notes.update_item"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    return serialize_item(update_item_record(db, claims.subject, item_id, payload, "agent"))


@app.delete("/api/agent/v1/items/{item_id}")
def agent_delete_item(
    item_id: str,
    claims: Annotated[AgentTokenClaims, Depends(require_agent_scope("notes.delete_item"))],
    db: Annotated[Session, Depends(get_db)],
) -> dict[str, object]:
    return delete_item_record(db, claims.subject, item_id, "agent")


@app.get("/static/{asset_path:path}")
def static_asset(asset_path: str) -> Response:
    if asset_path == "federated-banner.js":
        path = BANNER_SCRIPT_PATH
    else:
        root = WEB_DIR.resolve()
        path = (root / asset_path).resolve()
        if path != root and root not in path.parents:
            raise HTTPException(status_code=404)
    if not path.is_file():
        raise HTTPException(status_code=404)
    return Response(
        path.read_bytes(),
        media_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
    )


@app.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    if current_user(request, settings) is None:
        login = app_url(
            settings,
            f"/api/v1/auth/oauth/login?{urlencode({'next': app_url(settings, '/')})}",
        )
        return RedirectResponse(login, status_code=302)
    template = (WEB_DIR / "index.html").read_text(encoding="utf-8")
    base = settings.normalized_app_base_path
    rendered_html = template.replace("__APP_BASE_PATH__", json.dumps(base))
    base_href = f"{base}/" if base else "/"
    return HTMLResponse(
        rendered_html.replace("__APP_BASE_HREF__", html_module.escape(base_href, quote=True))
    )
