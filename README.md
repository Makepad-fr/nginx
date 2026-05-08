# Makepad Nginx

Shared Nginx reverse proxy deployment for Makepad-fr applications.

This repository owns the shared proxy stack for application VMs. Application repositories should not deploy Nginx directly. They should only attach their services to the shared application overlay network created or managed by this repository.

## Layout

- `compose.yml`: base Nginx service definition
- `sites/catwlk-*.conf.template`: Catwlk virtual host templates
- `sites/alerteconso-prod.conf.template`: Alerte Conso virtual host template
- `sites/le-petit-coin-prod.conf.template`: au petit coin backend virtual host template
- `envs/production/compose.yml`: production Swarm overrides
- `envs/production/.env.proxy`: production proxy settings

## Networks

The proxy joins a shared external overlay network:

- `${MAKEPAD_PROXY_PROD_APP_NETWORK}`
- `${MAKEPAD_PROXY_CANARY_APP_NETWORK}`
- `${MAKEPAD_PROXY_ALERTECONSO_APP_NETWORK}`
- `${MAKEPAD_PROXY_LE_PETIT_COIN_APP_NETWORK}`

Application stacks attach to the same external network and expose a stable alias such as `catwlk-app`.

## Node Labels

Pin the shared proxy to proxy-capable nodes:

```bash
docker node update --label-add infra.makepad.proxy=true <proxy-node>
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
- `MAKEPAD_PROXY_PROD_APP_NETWORK`
- `MAKEPAD_PROXY_CANARY_APP_NETWORK`
- `MAKEPAD_PROXY_ALERTECONSO_APP_NETWORK`
- `MAKEPAD_PROXY_LE_PETIT_COIN_APP_NETWORK`

The workflow deploys only the proxy stack. If the shared application network does not exist yet, it is created on the manager before deployment.

## TLS

Certificates must already exist on the proxy VM under `/etc/letsencrypt`, matching the paths configured in `envs/<environment>/.env.proxy`.

For `aupetitcoin.makepad.fr`, the production proxy expects:

- `/etc/letsencrypt/live/aupetitcoin.makepad.fr/fullchain.pem`
- `/etc/letsencrypt/live/aupetitcoin.makepad.fr/privkey.pem`
