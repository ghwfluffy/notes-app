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

## Assistant boundary

Assistant routes live below `/api/agent/v1`. Each operation requires a
short-lived HMAC `agent-v1` token whose audience is `notes`, whose subject is the
data owner, and whose scope exactly matches one capability. The assistant token
secret is shared out-of-band by deployment configuration. Browser cookies are
never accepted by assistant routes, and assistant tokens are never accepted by
browser routes.

## Federated banner

The app embeds the shared `vendor/federated-banner` package. Its authenticated
bootstrap response provides user display fields, account settings, and the
deployment-supplied non-secret `FEDERATED_APPS` inventory. Standalone operation
falls back to only Federated Services and My Notes.
