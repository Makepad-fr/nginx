# Makepad Nginx

Shared Nginx reverse proxy deployment for Makepad-fr applications.

This repository owns the shared proxy stack for application VMs. Application repositories should not deploy Nginx directly. They should only attach their services to the shared application overlay network created or managed by this repository.

## Layout

- `compose.yml`: base Nginx service definition
- `sites/catwlk.conf.template`: Catwlk virtual host template
- `envs/canary/compose.yml`: canary Swarm overrides
- `envs/canary/.env.proxy`: canary proxy settings
- `envs/production/compose.yml`: production Swarm overrides
- `envs/production/.env.proxy`: production proxy settings

## Networks

The proxy joins a shared external overlay network:

- `${DEPLOY_CATWLK_APP_NETWORK}`

Application stacks attach to the same external network and expose a stable alias such as `catwlk-app`.

## Node Labels

Pin the shared proxy to proxy-capable nodes:

```bash
docker node update --label-add infra.makepad.role=proxy <proxy-node>
```

## Deployment

Use the manual GitHub Actions workflow in this repository.

Required environment secrets:

- `DEPLOY_SSH_HOST`
- `DEPLOY_SSH_PORT`
- `DEPLOY_SSH_USER`
- `DEPLOY_SSH_PRIVATE_KEY`
- `DEPLOY_REMOTE_DIR`
- `DEPLOY_STACK_NAME`
- `DEPLOY_CATWLK_APP_NETWORK`

The workflow deploys only the proxy stack. If the shared application network does not exist yet, it is created on the manager before deployment.

## TLS

Certificates must already exist on the proxy VM under `/etc/certs`, matching the paths configured in `envs/<environment>/.env.proxy`.
