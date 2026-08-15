# Notes App Agent Instructions

- Read `docs/architecture.md` and `docs/development.md` before changing OAuth,
  persistence, browser routes, agent access, or list behavior.
- Keep the app deployment-neutral. Do not commit production hostnames, private
  production route prefixes, service aliases, or secrets.
- Central OAuth owns identity. Do not add local passwords or registration.
- Agent routes require an exact scoped `agent-v1` token and must always isolate
  data by the token subject.
- Add tests for behavioral changes and run `./scripts/validate.sh` before
  handing work back.
