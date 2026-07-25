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
- `sites/runtrace-prod.conf.template`: Runtrace production virtual host
- `sites/evidella-prod.conf.template`: Evidella landing site virtual host template
- `sites/backinmysize-prod.conf.template`: Back in My Size production virtual host
- `sites/scraping-admin-prod.conf.template`: Scraping crawler admin virtual host with app-owned GitHub edge authentication
- `sites/vestiaire-prod.conf.template`: Vestiaire web and Catalog API ingress on one origin
- `envs/production/compose.yml`: production Swarm overrides
- `envs/production/.env.proxy`: production proxy settings
- `scripts/render-swarm-stack.py`: content-addressed Swarm config renderer

## Networks

The proxy joins shared external overlay networks:

- `${MAKEPAD_PROXY_PROD_APP_NETWORK}`
- `${MAKEPAD_PROXY_CANARY_APP_NETWORK}`
- `${MAKEPAD_PROXY_ALERTECONSO_APP_NETWORK}`
- `${MAKEPAD_PROXY_LE_PETIT_COIN_APP_NETWORK}`
- `${MAKEPAD_PROXY_VIF_APP_NETWORK}`
- `${MAKEPAD_PROXY_MAKEPAD_LANDING_APP_NETWORK}`
- `${MAKEPAD_PROXY_RUNTRACE_APP_NETWORK}`
- `${MAKEPAD_PROXY_EVIDELLA_APP_NETWORK}`
- `${MAKEPAD_PROXY_BACKINMYSIZE_APP_NETWORK}`
- `${MAKEPAD_PROXY_SCRAPING_ADMIN_APP_NETWORK}`
- `${MAKEPAD_PROXY_VESTIAIRE_WEB_APP_NETWORK}`
- `${MAKEPAD_PROXY_VESTIAIRE_CATALOG_API_APP_NETWORK}`
- `${MAKEPAD_PROXY_VESTIAIRE_DEVELOPER_PLATFORM_APP_NETWORK}`

Each application stack attaches to its corresponding shared network and exposes a stable DNS alias there. `aupetitcoin.makepad.fr` proxies to `LE_PETIT_COIN_PROD_UPSTREAM`, which defaults to `http://le-petit-coin-backend:8080` to match the backend stack's production `LE_PETIT_COIN_BACKEND_ALIAS`. `makepad.fr` proxies to `MAKEPAD_LANDING_PROD_UPSTREAM`, which defaults to `http://makepad-landing-prod-app:8080`; `www.makepad.fr` redirects permanently to `makepad.fr`. `runtrace.co` proxies to `RUNTRACE_PROD_UPSTREAM`, which defaults to `http://runtrace-prod-app:8080`. `evidella.com` proxies to `EVIDELLA_PROD_UPSTREAM`, which defaults to `http://opsbrainlanding-prod-app:8080`; `www.evidella.com` redirects permanently to `evidella.com`. `backinmysize.com` proxies to `BACKINMYSIZE_PROD_UPSTREAM`, which defaults to `http://backinmysize-prod-app:8091`; `www.backinmysize.com` redirects permanently to `backinmysize.com`. `scraping.makepad.fr` proxies to `SCRAPING_ADMIN_PROD_UPSTREAM`, which defaults to `http://scraping-admin:8088`, after nginx validates the user session with the app-owned GitHub auth gate.

`vestiaire.io` uses path and representation routing across isolated application networks. `/developers`, `/current-user`, and `/organizations` are owned by the authenticated developer platform. `/healthz`, `/readyz`, `/product-offers/facets`, and malformed `/product-offers/` subresources always go to `VESTIAIRE_CATALOG_API_PROD_UPSTREAM`. `/product-offers` and `/product-offers/{64-character-offer-id}` use the request `Accept` header: JSON, `application/*`, an empty header, and `*/*` go to the Catalog API; HTML goes to `VESTIAIRE_WEB_PROD_UPSTREAM`; unsupported media types receive `406`. JSON wins when both HTML and JSON are advertised. Every other path goes to the web application. Negotiated responses include `Vary: Accept` so shared caches keep HTML and JSON representations separate. Upstream gateway failures become representation-appropriate `503 Service Unavailable` responses with `Retry-After: 60` and `Cache-Control: no-store`. Developer routes use a redacted access-log format that excludes OAuth query parameters and cookies.

Nginx terminates TLS for Vestiaire but does not authenticate Catalog API requests. The API validates Keycloak signatures and claims itself, so no identity-bearing header from an untrusted caller is accepted as authentication. Nginx forwards bearer tokens only to the Catalog API representation and strips them from web requests. API routes use a dedicated access-log format that records a normalized route, status, duration, and application request ID without query strings, product IDs, cookies, or authorization data.

The Scraping admin route is publicly reachable over HTTPS for the initial deployment, but requests are blocked until the Scraping app stack's GitHub username gate succeeds. When OpenConnexa is available, restrict `scraping.makepad.fr` at this proxy or network boundary without changing the Scraping admin upstream contract.

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
- `MAKEPAD_PROXY_RUNTRACE_APP_NETWORK`
- `MAKEPAD_PROXY_EVIDELLA_APP_NETWORK`
- `MAKEPAD_PROXY_BACKINMYSIZE_APP_NETWORK`
- `MAKEPAD_PROXY_OPENPANEL_APP_NETWORK`
- `MAKEPAD_PROXY_SCRAPING_ADMIN_APP_NETWORK`
- `MAKEPAD_PROXY_VESTIAIRE_WEB_APP_NETWORK`
- `MAKEPAD_PROXY_VESTIAIRE_CATALOG_API_APP_NETWORK`
- `MAKEPAD_PROXY_VESTIAIRE_DEVELOPER_PLATFORM_APP_NETWORK`

The workflow deploys only the proxy stack. If the shared application network does not exist yet, it is created on the manager before deployment. Nginx configuration objects use content-addressed names so Swarm can roll out immutable config changes and retain the previous objects for rollback.

Before any SSH or deployment step, `tests/vestiaire-routing.sh` starts isolated mock web and API upstreams and verifies the Vestiaire representation routes, cache variation, `406` behavior, credential isolation, upstream-unavailable fallbacks, and redacted access logs through a real Nginx container.

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

For `backinmysize.com` and `www.backinmysize.com`, the production proxy expects a certificate that covers both names:

- `/etc/letsencrypt/live/backinmysize.com/fullchain.pem`
- `/etc/letsencrypt/live/backinmysize.com/privkey.pem`

The `backinmysize.com` and `www.backinmysize.com` DNS records must point to the proxy VM before issuing the certificate or deploying the HTTPS route.

For `scraping.makepad.fr`, the production proxy expects:

- `/etc/letsencrypt/live/scraping.makepad.fr/fullchain.pem`
- `/etc/letsencrypt/live/scraping.makepad.fr/privkey.pem`

The `scraping.makepad.fr` DNS record must point to the proxy VM before issuing the certificate or deploying the HTTPS route.

For `vestiaire.io`, the production proxy expects:

- `/etc/letsencrypt/live/vestiaire.io/fullchain.pem`
- `/etc/letsencrypt/live/vestiaire.io/privkey.pem`

The `vestiaire.io` DNS record must point to the proxy VM before issuing the certificate or deploying the HTTPS route. The web stack must publish the `vestiaire-web` alias on `MAKEPAD_PROXY_VESTIAIRE_WEB_APP_NETWORK`; the Catalog API stack must publish `vestiaire-catalog-api` on `MAKEPAD_PROXY_VESTIAIRE_CATALOG_API_APP_NETWORK`; the developer platform must publish `vestiaire-developer-platform` on `MAKEPAD_PROXY_VESTIAIRE_DEVELOPER_PLATFORM_APP_NETWORK`.
