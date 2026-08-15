# Notes App

A small, mobile-first app for keeping private lists such as movies to watch,
games, project ideas, date ideas, and quotes. It uses central OAuth for browser
sessions and exposes a separately scoped API so the GHWIZ assistant can manage
the signed-in owner's lists. Compact navigation and plain, separator-based text
rows preserve usable touch targets without card padding consuming the mobile
screen. Visible up/down controls move note items within their current active or
completed section, while lists can be reordered from a numbered drag-and-arrow
sheet or by asking the assistant to arrange them. Items remain in the list where
they were created.

The repository is deployment-neutral. Its default browser prefix is `/notes`;
the private production prefix is supplied only by the parent deployment repo.

## Development

```sh
cp .env.example .env
docker compose up --build
```

Open `http://localhost:8096/notes/`. A central OAuth service is required for an
interactive browser login; update the two OAuth URLs in `.env` when that service
is not running at `http://localhost:8090/auth`.

## Validation

```sh
./scripts/validate.sh
```

See [Architecture](docs/architecture.md) and [Development](docs/development.md).
