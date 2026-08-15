from __future__ import annotations

import json
import re
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.agent_tokens import encode_agent_token
from app.config import Settings, get_settings, validate_production
from app.db import Base, get_db
from app.main import app, serializer
from app.models import AuditEvent


TEST_SECRET = "integration-secret-for-notes-tests-1234567890"
settings = Settings(
    app_env="test",
    app_base_path="",
    public_url="http://testserver",
    auth_base_url="/auth",
    oauth_server_base_url="http://central-api:8000",
    session_key="session-key-for-notes-tests-1234567890",
    agent_integration_token_secret=TEST_SECRET,
    federated_apps=json.dumps(
        [
            {
                "slug": "federated-services",
                "name": "Federated Services",
                "baseUrl": "/auth?tab=apps",
                "description": "Identity",
            },
            {
                "slug": "notes",
                "name": "My Notes",
                "baseUrl": "/notes",
                "description": "Lists",
            },
        ]
    ),
)

test_engine = create_engine(
    "sqlite+pysqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSession = sessionmaker(bind=test_engine, expire_on_commit=False, class_=Session)


def override_db() -> Generator[Session, None, None]:
    db = TestingSession()
    try:
        yield db
    finally:
        db.close()


def override_settings() -> Settings:
    return settings


app.dependency_overrides[get_db] = override_db
app.dependency_overrides[get_settings] = override_settings
client = TestClient(app)


@pytest.fixture(autouse=True)
def reset_database() -> Generator[None, None, None]:
    Base.metadata.drop_all(test_engine)
    Base.metadata.create_all(test_engine)
    client.cookies.clear()
    yield


def authenticate(subject: str = "owner-1", name: str = "Owner") -> None:
    client.cookies.set(
        settings.session_cookie_name,
        serializer(settings).dumps(
            {
                "sub": subject,
                "name": name,
                "preferred_username": subject,
                "email": f"{subject}@example.test",
            }
        ),
    )


def agent_headers(subject: str, scope: str) -> dict[str, str]:
    token = encode_agent_token(secret=TEST_SECRET, subject=subject, scope=scope)
    return {"Authorization": f"Bearer {token}"}


def test_browser_requires_auth_and_bootstraps_starter_lists() -> None:
    assert client.get("/", follow_redirects=False).status_code == 302
    assert client.get("/api/v1/lists").status_code == 401

    authenticate()
    index = client.get("/")
    assert index.status_code == 200
    assert '<base href="/">' in index.text

    response = client.get("/api/v1/lists")
    assert response.status_code == 200
    lists = response.json()["lists"]
    assert [value["name"] for value in lists] == [
        "Movies to Watch",
        "Games",
        "Project Ideas",
        "Date Ideas",
        "Quotes",
    ]

    me = client.get("/api/v1/auth/me").json()
    assert me["user"]["subject"] == "owner-1"
    assert {entry["slug"] for entry in me["federatedApps"]} == {
        "federated-services",
        "notes",
    }


def test_browser_crud_and_deleted_starters_do_not_reappear() -> None:
    authenticate()
    starter_lists = client.get("/api/v1/lists").json()["lists"]
    movies = starter_lists[0]

    created = client.post(
        f"/api/v1/lists/{movies['id']}/items",
        json={"title": "Arrival", "details": "Watch again with commentary"},
    )
    assert created.status_code == 201
    item = created.json()
    assert item["completed"] is False

    updated = client.patch(
        f"/api/v1/items/{item['id']}",
        json={"completed": True, "title": "Arrival (2016)"},
    )
    assert updated.status_code == 200
    assert updated.json()["completed"] is True

    custom = client.post(
        "/api/v1/lists",
        json={"name": "Restaurants", "description": "Places to try", "color": "#123abc"},
    )
    assert custom.status_code == 201
    assert custom.json()["color"] == "#123abc"

    for note_list in client.get("/api/v1/lists").json()["lists"]:
        assert client.delete(f"/api/v1/lists/{note_list['id']}").status_code == 200
    assert client.get("/api/v1/lists").json()["lists"] == []

    first = client.post("/api/v1/lists", json={"name": "First"}).json()
    second = client.post("/api/v1/lists", json={"name": "Second"}).json()
    assert (first["position"], second["position"]) == (0, 1)


def test_browser_rejects_moving_an_item_to_another_list() -> None:
    authenticate()
    lists = client.get("/api/v1/lists").json()["lists"]
    source, destination = lists[:2]
    item = client.post(
        f"/api/v1/lists/{source['id']}/items",
        json={"title": "Keep me here"},
    ).json()

    response = client.patch(
        f"/api/v1/items/{item['id']}",
        json={"list_id": destination["id"]},
    )

    assert response.status_code == 422
    assert any(error["loc"][-1] == "list_id" for error in response.json()["detail"])
    reloaded = client.get("/api/v1/lists").json()["lists"]
    assert [entry["id"] for entry in reloaded[0]["items"]] == [item["id"]]
    assert reloaded[1]["items"] == []


def test_browser_item_position_patch_keeps_a_contiguous_order() -> None:
    authenticate()
    note_list = client.get("/api/v1/lists").json()["lists"][0]
    items = [
        client.post(
            f"/api/v1/lists/{note_list['id']}/items",
            json={"title": title},
        ).json()
        for title in ("First", "Second", "Third")
    ]

    moved = client.patch(f"/api/v1/items/{items[2]['id']}", json={"position": 0})

    assert moved.status_code == 200
    reordered = client.get("/api/v1/lists").json()["lists"][0]["items"]
    assert [item["id"] for item in reordered] == [
        items[2]["id"],
        items[0]["id"],
        items[1]["id"],
    ]
    assert [item["position"] for item in reordered] == [0, 1, 2]
    assert (
        client.patch(f"/api/v1/items/{items[0]['id']}", json={"position": 3}).status_code
        == 422
    )

    with TestingSession() as db:
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.owner_subject == "owner-1",
                AuditEvent.action == "item.updated",
                AuditEvent.entity_id == items[2]["id"],
            )
        )
        assert event is not None
        assert event.details == {
            "fields": ["position"],
            "list_id": note_list["id"],
        }


def test_browser_reload_returns_custom_lists_and_items_with_starters() -> None:
    authenticate()
    starter_names = {
        note_list["name"] for note_list in client.get("/api/v1/lists").json()["lists"]
    }

    custom = client.post(
        "/api/v1/lists",
        json={"name": "News to Share", "description": "Interesting finds", "color": "#38608f"},
    ).json()
    item = client.post(
        f"/api/v1/lists/{custom['id']}/items",
        json={"title": "A cool discovery", "details": "Send this one to the owner"},
    ).json()

    reloaded_lists = client.get("/api/v1/lists").json()["lists"]
    assert starter_names <= {note_list["name"] for note_list in reloaded_lists}
    reloaded_custom = next(note_list for note_list in reloaded_lists if note_list["id"] == custom["id"])
    assert reloaded_custom["name"] == "News to Share"
    assert reloaded_custom["description"] == "Interesting finds"
    assert reloaded_custom["active_item_count"] == 1
    assert reloaded_custom["items"] == [item]


def test_browser_reorders_every_list_atomically_and_audits_the_change() -> None:
    authenticate()
    lists = client.get("/api/v1/lists").json()["lists"]
    desired_ids = [lists[2]["id"], lists[0]["id"], lists[1]["id"], lists[4]["id"], lists[3]["id"]]

    response = client.put("/api/v1/lists/order", json={"list_ids": desired_ids})

    assert response.status_code == 200
    assert [note_list["id"] for note_list in response.json()["lists"]] == desired_ids
    assert [note_list["position"] for note_list in response.json()["lists"]] == list(range(5))
    assert [note_list["id"] for note_list in client.get("/api/v1/lists").json()["lists"]] == desired_ids

    duplicate = client.put(
        "/api/v1/lists/order",
        json={"list_ids": [desired_ids[0], desired_ids[0], *desired_ids[2:]]},
    )
    assert duplicate.status_code == 422
    missing = client.put("/api/v1/lists/order", json={"list_ids": desired_ids[:-1]})
    assert missing.status_code == 409
    assert [note_list["id"] for note_list in client.get("/api/v1/lists").json()["lists"]] == desired_ids

    with TestingSession() as db:
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.owner_subject == "owner-1",
                AuditEvent.action == "lists.reordered",
            )
        )
        assert event is not None
        assert event.actor_type == "user"
        assert event.details == {"list_ids": desired_ids, "changed_count": 5}


def test_single_list_position_patch_keeps_a_contiguous_order() -> None:
    authenticate()
    lists = client.get("/api/v1/lists").json()["lists"]

    moved = client.patch(f"/api/v1/lists/{lists[4]['id']}", json={"position": 1})

    assert moved.status_code == 200
    reordered = client.get("/api/v1/lists").json()["lists"]
    assert [note_list["id"] for note_list in reordered] == [
        lists[0]["id"],
        lists[4]["id"],
        lists[1]["id"],
        lists[2]["id"],
        lists[3]["id"],
    ]
    assert [note_list["position"] for note_list in reordered] == list(range(5))
    assert client.patch(f"/api/v1/lists/{lists[0]['id']}", json={"position": 5}).status_code == 422


def test_browser_loading_state_is_hidden_correctly_and_requests_are_bounded() -> None:
    authenticate()
    index = client.get("/").text
    css = client.get("/static/app.css").text
    javascript = client.get("/static/app.js").text

    assert 'id="loading-state"' in index
    assert re.search(r'id="app-layout"[^>]*\shidden(?:[\s>])', index)
    assert re.search(r'id="fatal-error"[^>]*\shidden(?:[\s>])', index)
    assert re.search(r"\[hidden\]\s*\{\s*display:\s*none\s*!important;\s*\}", css)
    assert "const REQUEST_TIMEOUT_MS = 15000;" in javascript
    assert "const controller = new AbortController();" in javascript
    assert "window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS)" in javascript
    assert "signal: controller.signal" in javascript
    assert "window.clearTimeout(timeoutId)" in javascript
    assert "My Notes took too long to respond. Try again." in javascript
    assert "My Notes could not connect. Check your connection and try again." in javascript
    assert "const loadAttempt = ++latestLoadAttempt;" in javascript
    assert javascript.count("if (loadAttempt !== latestLoadAttempt) return;") == 2
    assert "loadingState.hidden = false;\n    fatalError.hidden = true;\n    appLayout.hidden = true;" in javascript
    assert (
        "loadingState.hidden = true;\n"
        "      fatalError.hidden = true;\n"
        "      appLayout.hidden = false;"
    ) in javascript
    assert (
        "loadingState.hidden = true;\n"
        "      fatalError.hidden = false;\n"
        "      appLayout.hidden = true;"
    ) in javascript
    assert "if (response.status === 401)" in javascript
    assert "/auth/oauth/login?next=" in javascript
    assert 'id="reorder-dialog"' in index
    assert 'id="reorder-lists-button"' in index
    assert 'request("/lists/order"' in javascript
    assert 'method: "PUT"' in javascript
    assert 'body: JSON.stringify({ list_ids: state.reorderListIds })' in javascript
    assert 'grip.addEventListener("pointerdown"' in javascript
    assert 'class="reorder-help"' in index
    assert 'class="reorder-list"' in index
    assert '"reorder-position"' in javascript
    assert '"reorder-grip-icon"' in javascript
    assert 'aria-keyshortcuts' in javascript
    assert 'focusReorderControl(listId' in javascript
    assert "requestedControl && !requestedControl.disabled" in javascript
    assert 'announceReorder(listId)' in javascript
    assert 'up.dataset.direction = "up"' in javascript
    assert 'down.dataset.direction = "down"' in javascript
    assert 'controls.setAttribute("role", "group")' in javascript
    assert "up.title = `Move ${noteList.name} up`" in javascript
    assert "down.title = `Move ${noteList.name} down`" in javascript
    assert "touch-action: none" in css
    assert "font-size: 0.88rem" in css
    assert "padding: 0.4rem 0.5rem" in css
    assert "border-bottom: 1px solid var(--line)" in css
    assert "border-radius: 0" in css
    assert "font-size: 0.9rem" in css
    assert '"item-move-controls"' in javascript
    assert 'up.dataset.itemDirection = "up"' in javascript
    assert 'down.dataset.itemDirection = "down"' in javascript
    assert "body: JSON.stringify({ position: target.position })" in javascript
    assert 'edit.classList.add("item-edit-button")' in javascript
    assert 'id="item-list"' not in index
    assert 'byId("item-list")' not in javascript
    assert "list_id: byId" not in javascript


def test_browser_starts_with_compact_list_navigation_and_trailing_actions() -> None:
    authenticate()
    index = client.get("/").text
    css = client.get("/static/app.css").text
    javascript = client.get("/static/app.js").text

    assert 'id="page-title"' not in index
    assert 'class="page-heading"' not in index
    assert 'class="subtitle"' not in index
    assert 'class="sidebar-label"' not in index
    assert 'type="search"' not in index
    assert 'id="search-input"' not in index
    assert 'id="clear-search"' not in index
    assert "searchInput" not in javascript
    assert "clearSearch" not in javascript
    assert "renderSearchResults" not in javascript
    assert "state.query" not in javascript
    assert ".page-heading" not in css
    assert ".page-actions" not in css
    assert ".search-field" not in css
    assert ".search-icon" not in css
    assert ".clear-search" not in css

    navigation = re.search(
        r'<nav id="list-navigation" class="list-navigation">(.*?)</nav>',
        index,
        re.DOTALL,
    )
    assert navigation is not None
    navigation_markup = navigation.group(1)
    new_list_position = navigation_markup.index('id="new-list-button"')
    reorder_position = navigation_markup.index('id="reorder-lists-button"')
    assert new_list_position < reorder_position
    assert "New List" in navigation_markup
    assert "Reorder" in navigation_markup
    assert 'aria-label="Create a new list"' in navigation_markup
    assert 'aria-label="Reorder lists"' in navigation_markup
    assert re.search(r'id="reorder-lists-button"[^>]*\sdisabled', navigation_markup)
    assert "navigationItems.push(button);" in javascript
    assert (
        "listNavigation.replaceChildren(...navigationItems, newListButton, reorderListsButton);"
        in javascript
    )
    assert javascript.index("navigationItems.push(button);") < javascript.index(
        "listNavigation.replaceChildren(...navigationItems, newListButton, reorderListsButton);"
    )
    assert "reorderListsButton.disabled = state.lists.length < 2;" in javascript
    assert 'newListButton.addEventListener("click", () => openListDialog());' in javascript
    assert 'reorderListsButton.addEventListener("click", openReorderDialog);' in javascript

    mobile_css = css[
        css.index("@media (max-width: 720px)") : css.index("@media (max-width: 430px)")
    ]
    assert "overflow-x: auto" in mobile_css
    assert ".list-action-button" in mobile_css
    assert "flex: 0 0 auto" in mobile_css
    assert "width: auto" in mobile_css


def test_browser_data_is_isolated_by_oauth_subject() -> None:
    authenticate("first-owner")
    first_list = client.get("/api/v1/lists").json()["lists"][0]
    client.post(f"/api/v1/lists/{first_list['id']}/items", json={"title": "Private item"})

    authenticate("second-owner")
    second_lists = client.get("/api/v1/lists").json()["lists"]
    assert all(not note_list["items"] for note_list in second_lists)
    assert client.get(f"/api/v1/lists/{first_list['id']}").status_code == 405
    assert client.delete(f"/api/v1/lists/{first_list['id']}").status_code == 404


def test_agent_routes_require_exact_scope_and_audit_agent_writes() -> None:
    list_response = client.get(
        "/api/agent/v1/lists",
        headers=agent_headers("owner-agent", "notes.list_lists"),
    )
    assert list_response.status_code == 200
    movies = list_response.json()["lists"][0]

    wrong_scope = client.post(
        f"/api/agent/v1/lists/{movies['id']}/items",
        headers=agent_headers("owner-agent", "notes.list_lists"),
        json={"title": "The Wild Robot"},
    )
    assert wrong_scope.status_code == 401

    created = client.post(
        f"/api/agent/v1/lists/{movies['id']}/items",
        headers=agent_headers("owner-agent", "notes.create_item"),
        json={"title": "The Wild Robot"},
    )
    assert created.status_code == 201

    other_owner_items = client.get(
        f"/api/agent/v1/lists/{movies['id']}/items",
        headers=agent_headers("different-owner", "notes.list_items"),
    )
    assert other_owner_items.status_code == 404

    with TestingSession() as db:
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.owner_subject == "owner-agent",
                AuditEvent.action == "item.created",
            )
        )
        assert event is not None
        assert event.actor_type == "agent"


def test_agent_rejects_moving_an_item_to_another_list() -> None:
    lists = client.get(
        "/api/agent/v1/lists",
        headers=agent_headers("owner-agent", "notes.list_lists"),
    ).json()["lists"]
    source, destination = lists[:2]
    item = client.post(
        f"/api/agent/v1/lists/{source['id']}/items",
        headers=agent_headers("owner-agent", "notes.create_item"),
        json={"title": "Still belongs here"},
    ).json()

    response = client.patch(
        f"/api/agent/v1/items/{item['id']}",
        headers=agent_headers("owner-agent", "notes.update_item"),
        json={"list_id": destination["id"]},
    )

    assert response.status_code == 422
    assert any(error["loc"][-1] == "list_id" for error in response.json()["detail"])
    source_items = client.get(
        f"/api/agent/v1/lists/{source['id']}/items",
        headers=agent_headers("owner-agent", "notes.list_items"),
    ).json()["items"]
    destination_items = client.get(
        f"/api/agent/v1/lists/{destination['id']}/items",
        headers=agent_headers("owner-agent", "notes.list_items"),
    ).json()["items"]
    assert [entry["id"] for entry in source_items] == [item["id"]]
    assert destination_items == []


def test_agent_can_reorder_only_its_owners_lists_with_exact_scope() -> None:
    lists = client.get(
        "/api/agent/v1/lists",
        headers=agent_headers("owner-agent", "notes.list_lists"),
    ).json()["lists"]
    desired_ids = [note_list["id"] for note_list in reversed(lists)]

    wrong_scope = client.put(
        "/api/agent/v1/lists/order",
        headers=agent_headers("owner-agent", "notes.update_list"),
        json={"list_ids": desired_ids},
    )
    assert wrong_scope.status_code == 401

    response = client.put(
        "/api/agent/v1/lists/order",
        headers=agent_headers("owner-agent", "notes.reorder_lists"),
        json={"list_ids": desired_ids},
    )
    assert response.status_code == 200
    assert [note_list["id"] for note_list in response.json()["lists"]] == desired_ids
    assert all(note_list["items"] == [] for note_list in response.json()["lists"])

    foreign = client.put(
        "/api/agent/v1/lists/order",
        headers=agent_headers("different-owner", "notes.reorder_lists"),
        json={"list_ids": desired_ids},
    )
    assert foreign.status_code == 409

    with TestingSession() as db:
        event = db.scalar(
            select(AuditEvent).where(
                AuditEvent.owner_subject == "owner-agent",
                AuditEvent.action == "lists.reordered",
            )
        )
        assert event is not None
        assert event.actor_type == "agent"


def test_production_configuration_rejects_documentation_placeholders() -> None:
    configured = Settings(
        app_env="production",
        public_url="https://notes.example.test",
        postgres_password="change-me",
        session_key="replace-with-at-least-32-random-characters",
        agent_integration_token_secret="replace-with-the-shared-agent-integration-secret",
    )

    with pytest.raises(ValueError, match="SESSION_KEY, POSTGRES_PASSWORD, AGENT_INTEGRATION_TOKEN_SECRET"):
        validate_production(configured)
