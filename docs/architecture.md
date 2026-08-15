# Architecture

My Notes is a deliberately small FastAPI application with a mobile-first,
framework-free browser client. PostgreSQL stores private user lists, list items,
one-time starter-list initialization, and write audit events. Alembic is the
only schema migration mechanism.

## Identity and ownership

Central OAuth is the only interactive identity source. The browser keeps a
short, signed consumer-app session containing the central subject and basic
display fields; no password or registration flow exists here. Every database
query is constrained by that subject.

The browser prefix, OAuth endpoints, public origin, cookie path, and central
Auth locations derive from configuration. The app's default prefix is `/notes`.
Private production routing belongs to the parent deployment repo and must not be
committed here.

The HTTP boundary accepts requests with `APP_BASE_PATH` still attached as well
as requests from an ingress that has already stripped it. Generated browser,
cookie, and callback URLs always retain the configured public prefix.

## Lists

On a user's first authenticated list request, the API records initialization and
creates five starter lists: Movies to Watch, Games, Project Ideas, Date Ideas,
and Quotes. Initialization is separate from list existence, so deliberately
deleting every list does not cause the starters to reappear.

Lists and items have stable UUID identifiers and explicit positions. Deleting a
list cascades to its items. Meaningful writes create an audit row with the owner,
actor type (`user`, `agent`, or `system`), action, entity, and bounded metadata.

The browser keeps list navigation deliberately compact and renders notes as
plain text rows separated by a thin rule, without card backgrounds, rounded
boxes, or container padding. Checkbox, text, and ordering controls retain
separate 44px interaction heights. Visible up/down controls move an item within
its active or completed section and restore focus after the list reloads. The
dedicated list reorder sheet is an ordered-row
editor with visible position numbers, a labeled touch/mouse drag handle, compact
move-up/down fallbacks, keyboard Arrow/Home/End handling, focus restoration, and
live-region position announcements. It saves the complete ordered id set in one
request. The API locks the owner's current list rows, requires every current list
exactly once, assigns contiguous positions, and rejects stale or partial orders
without changing anything. This same atomic contract is available to the
assistant through the exact `notes.reorder_lists` scope.

An item's parent list is fixed when the item is created. Item editing can change
title, details, completion, or position within that list. Position updates lock
the current list order, move the selected item, and rewrite contiguous positions
so repeated browser or assistant moves cannot create duplicate slots. Neither
the browser nor assistant update contract offers cross-list movement. Item patch
schemas forbid `list_id`, so older or hand-built browser and scoped-assistant
requests cannot bypass that product rule.

## Browser loading lifecycle

The browser starts with the loading card visible and both the app layout and
fatal-error card hidden. A global `[hidden] { display: none !important; }` rule
is part of the frontend state contract: component and responsive `display`
rules must never override those state transitions.

Initial loading requests the authenticated user and `/api/v1/lists` together.
Every browser API request has a 15-second deadline implemented with an
`AbortController`, and its timer is cleared after the request settles. A `401`
continues to redirect through central OAuth. On success, the response hydrates
all starter and owner-created lists and their items before the layout replaces
the loading card. An owner with no lists sees the normal in-layout empty state.
Timeouts, network failures, and API errors replace the loading card with a
bounded error state and retry action; loading, content, and fatal error are
therefore mutually exclusive. A retry supersedes any earlier bootstrap attempt
so a stale response cannot replace the newest loading result.

## Assistant boundary

Assistant routes live below `/api/agent/v1`. Each operation requires a
short-lived HMAC `agent-v1` token whose audience is `notes`, whose subject is the
data owner, and whose scope exactly matches one capability. The assistant token
secret is shared out-of-band by deployment configuration. Browser cookies are
never accepted by assistant routes, and assistant tokens are never accepted by
browser routes. List reordering requires a complete list-id order, so a model
must first read the current lists and cannot omit, duplicate, or import another
owner's list while moving one list. Item updates cannot change `list_id`; the
assistant may edit or reorder an item only within its creation list.

## Federated banner

The app embeds the shared `vendor/federated-banner` package. Its authenticated
bootstrap response provides user display fields, account settings, and the
deployment-supplied non-secret `FEDERATED_APPS` inventory. Standalone operation
falls back to only Federated Services and My Notes.
