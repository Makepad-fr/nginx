# Repository Conventions

## Deploy Layout

- This repository owns the shared Nginx reverse proxy stack.
- Application repositories should attach to the shared external overlay network created here instead of deploying their own proxy.
- Canary and production overrides live under `envs/<environment>/compose.yml`.
- Proxy env files live under `envs/<environment>/.env.proxy`.

## Placement

- Proxy services are pinned with `node.labels.infra.makepad.role == proxy`.

## Documentation

- Keep `README.md` and workflow instructions aligned with network names, certificate paths, and deployment steps.
