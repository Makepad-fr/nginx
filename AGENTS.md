# Repository Conventions

## Deploy Layout

- This repository owns the shared Nginx reverse proxy stack.
- Application repositories should attach to their shared external overlay network created here instead of deploying their own proxy.
- Use app-scoped `MAKEPAD_PROXY_*_APP_NETWORK` secret names in this shared repo, matching each application's `DEPLOY_APP_NETWORK` value.
- Canary and production overrides live under `envs/<environment>/compose.yml`.
- Proxy env files live under `envs/<environment>/.env.proxy`.

## Placement

- Proxy services are pinned with `node.labels.infra.makepad.proxy == true`.

## Documentation

- Keep `README.md` and workflow instructions aligned with network names, certificate paths, and deployment steps.
