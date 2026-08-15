# Development

## Configuration

Copy `.env.example` to `.env` and replace development credentials. The app is
deployment-neutral; use a local public origin and route prefix.

`docker compose up --build` starts PostgreSQL, applies migrations, and serves
the app on port 8096. The example expects a compatible central OAuth provider at
`http://localhost:8090/auth`; adjust the public and container-reachable OAuth
URLs when using another development deployment.

## Database and server

Run migrations from `api/`:

```sh
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

The browser requires a compatible central OAuth provider. Production ingress
may strip the configured public prefix before forwarding requests because every
browser URL emitted by the app includes `APP_BASE_PATH`.

## Tests and lint

```sh
./scripts/validate.sh
```

Tests use an isolated in-memory SQLite database and dependency overrides; they
do not require PostgreSQL or an OAuth server.
