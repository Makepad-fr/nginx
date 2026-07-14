# Makepad Nginx

Shared Nginx reverse proxy deployment for Makepad-fr applications.

This repository owns the shared proxy stack for application VMs. Application repositories should not deploy Nginx directly. They should only attach their services to the shared application overlay network created or managed by this repository.

## Layout

- `compose.yml`: base Nginx service definition
- `sites/catwlk-*.conf.template`: Catwlk virtual host templates
- `sites/alerteconso-prod.conf.template`: Alerte Conso virtual host template
- `sites/le-petit-coin-prod.conf.template`: au petit coin backend virtual host template
- `sites/vif-prod.conf.template`: Vif virtual host template
- `sites/makepad-landing-prod.conf.template`: Makepad landing site virtual host template
- `sites/runtrace-prod.conf.template`: inactive Runtrace virtual host template, kept until DNS and TLS are ready
- `sites/evidella-prod.conf.template`: Evidella landing site virtual host template
- `sites/fashion-crawler-admin-prod.conf.template`: Fashion crawler admin virtual host with Keycloak-backed oauth2-proxy authentication
- `envs/production/compose.yml`: production Swarm overrides
- `envs/production/.env.proxy`: production proxy settings

## Networks

The proxy joins shared external overlay networks:

- `${MAKEPAD_PROXY_PROD_APP_NETWORK}`
- `${MAKEPAD_PROXY_CANARY_APP_NETWORK}`
- `${MAKEPAD_PROXY_ALERTECONSO_APP_NETWORK}`
- `${MAKEPAD_PROXY_LE_PETIT_COIN_APP_NETWORK}`
- `${MAKEPAD_PROXY_VIF_APP_NETWORK}`
- `${MAKEPAD_PROXY_MAKEPAD_LANDING_APP_NETWORK}`
- `${MAKEPAD_PROXY_EVIDELLA_APP_NETWORK}`
- `${MAKEPAD_PROXY_FASHION_CRAWLER_ADMIN_APP_NETWORK}`

Each application stack attaches to its corresponding shared network and exposes a stable DNS alias there. `aupetitcoin.makepad.fr` proxies to `LE_PETIT_COIN_PROD_UPSTREAM`, which defaults to `http://le-petit-coin-backend:8080` to match the backend stack's production `LE_PETIT_COIN_BACKEND_ALIAS`. `makepad.fr` proxies to `MAKEPAD_LANDING_PROD_UPSTREAM`, which defaults to `http://makepad-landing-prod-app:8080`; `www.makepad.fr` redirects permanently to `makepad.fr`. `evidella.com` proxies to `EVIDELLA_PROD_UPSTREAM`, which defaults to `http://opsbrainlanding-prod-app:8080`; `www.evidella.com` redirects permanently to `evidella.com`. `fashion.makepad.fr` proxies to `FASHION_CRAWLER_ADMIN_PROD_UPSTREAM`, which defaults to `http://fashion-crawler-admin:8088`, after nginx validates the user session with the `fashion-crawler-admin-oauth2-proxy` sidecar.

The Fashion admin route is publicly reachable over HTTPS for the initial deployment, but requests are blocked until Keycloak authentication succeeds. When OpenConnexa is available, restrict `fashion.makepad.fr` at this proxy or network boundary without changing the Fashion admin upstream contract.

## Node Labels

Pin the shared proxy to proxy-capable nodes:

```bash
docker node update --label-add infra.makepad.proxy=true <proxy-node>
```

## Deployment

The deploy workflow runs automatically on pushes to `main` that change the proxy Compose files, production environment, site templates, or the deploy workflow itself. It can also be run manually from GitHub Actions.

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
- `MAKEPAD_PROXY_VIF_APP_NETWORK`
- `MAKEPAD_PROXY_MAKEPAD_LANDING_APP_NETWORK`
- `MAKEPAD_PROXY_EVIDELLA_APP_NETWORK`
- `MAKEPAD_PROXY_FASHION_CRAWLER_ADMIN_APP_NETWORK`
- `FASHION_CRAWLER_ADMIN_CLIENT_SECRET`
- `FASHION_CRAWLER_ADMIN_OAUTH_COOKIE_SECRET`

The workflow deploys only the proxy stack. If the shared application network does not exist yet, it is created on the manager before deployment.

## TLS

Certificates must already exist on the proxy VM under `/etc/letsencrypt`, matching the paths configured in `envs/<environment>/.env.proxy`.

For `aupetitcoin.makepad.fr`, the production proxy expects:

- `/etc/letsencrypt/live/aupetitcoin.makepad.fr/fullchain.pem`
- `/etc/letsencrypt/live/aupetitcoin.makepad.fr/privkey.pem`

For `makepad.fr` and `www.makepad.fr`, the production proxy expects a certificate that covers both names:

- `/etc/letsencrypt/live/makepad.fr/fullchain.pem`
- `/etc/letsencrypt/live/makepad.fr/privkey.pem`

For `evidella.com` and `www.evidella.com`, the production proxy expects a certificate that covers both names:

- `/etc/letsencrypt/live/evidella.com/fullchain.pem`
- `/etc/letsencrypt/live/evidella.com/privkey.pem`

The `evidella.com` and `www.evidella.com` DNS records must point to the proxy VM before issuing the certificate or deploying the HTTPS route.

For `fashion.makepad.fr`, the production proxy expects:

- `/etc/letsencrypt/live/fashion.makepad.fr/fullchain.pem`
- `/etc/letsencrypt/live/fashion.makepad.fr/privkey.pem`

The `fashion.makepad.fr` DNS record must point to the proxy VM before issuing the certificate or deploying the HTTPS route.
