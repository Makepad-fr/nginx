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
- `sites/runtrace-prod.conf.template`: Runtrace virtual host template for `runtrace.co`
- `sites/brio-staging.conf.template`: private Brio staging application host
- `sites/maildev-brio-staging.conf.template`: maintainer-only Brio MailDev host
- `sites/evidella-prod.conf.template`: Evidella landing site virtual host template
- `sites/openpanel-prod.conf.template`: OpenPanel analytics virtual host template
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
- `${MAKEPAD_PROXY_OPENPANEL_APP_NETWORK}`
- `${MAKEPAD_PROXY_RUNTRACE_APP_NETWORK}`
- `${MAKEPAD_PROXY_BRIO_STAGING_APP_NETWORK}`
- `${MAKEPAD_PROXY_MAILDEV_BRIO_STAGING_WEB_NETWORK}`

Each application stack attaches to its corresponding shared network and exposes a stable DNS alias there. `aupetitcoin.makepad.fr` proxies to `LE_PETIT_COIN_PROD_UPSTREAM`, which defaults to `http://le-petit-coin-backend:8080` to match the backend stack's production `LE_PETIT_COIN_BACKEND_ALIAS`. `makepad.fr` proxies to `MAKEPAD_LANDING_PROD_UPSTREAM`, which defaults to `http://makepad-landing-prod-app:8080`; `www.makepad.fr` redirects permanently to `makepad.fr`. `evidella.com` proxies to `EVIDELLA_PROD_UPSTREAM`, which defaults to `http://opsbrainlanding-prod-app:8080`; `www.evidella.com` redirects permanently to `evidella.com`.
`runtrace.co` proxies to `RUNTRACE_PROD_UPSTREAM`, which defaults to `http://runtrace-prod-app:8080`; the Runtrace app stack must attach to the same network value as `${MAKEPAD_PROXY_RUNTRACE_APP_NETWORK}`.

The Brio staging application uses an encrypted edge overlay shared only with
Nginx; the MailDev UI uses a separate internal, encrypted overlay. New Brio
overlays are created with explicit `--opt encrypted=true`; Docker records a
valueless `--opt encrypted` as an empty option rather than `true`. MailDev is protected by the companion GitHub OAuth
gate; every UI, API, and WebSocket request requires an allowlisted maintainer,
and the relay endpoint is denied at the proxy. Both hosts use a privacy access
log that omits paths, query strings, client addresses, referrers, and user-agent
values. Run `scripts/test-brio-staging-policy.sh` before deployment.

The Runtrace virtual host permits request bodies up to 64 MiB only on `POST /telemetry-batches`. Other Runtrace routes retain a 4 MiB proxy ceiling. Per-client request and connection budgets isolate fleet ingestion from normal administrator and public traffic; rejected excess traffic receives HTTP 429. Nginx generates one `X-Request-ID`, forwards it to Runtrace, and returns it on every response. The Runtrace access log is structured JSON with that ID, method, status, byte counts, and timing only; it intentionally excludes URLs, query strings, IP addresses, headers, bodies, and tenant identifiers. The production service also has CPU/memory reservations and limits plus bounded JSON logs. Before deployment, run `scripts/test-runtrace-upload-policy.sh`; it validates these controls, renders the production template, validates it with nginx, proves request-ID correlation and log privacy, and exercises the accepted and rejected upload boundaries through disposable containers.

## Node Labels

Pin the shared proxy to proxy-capable nodes:

```bash
docker node update --label-add infra.makepad.proxy=true <proxy-node>
```

## Deployment

The deploy workflow is manual and accepts only the exact commit selected from
protected `main`. It runs in the repository-restricted `Nginx Deploy`
organization runner group on a dedicated self-hosted runner; ordinary CI uses
the separate `Nginx CI` group and never receives deployment credentials. Keep
both groups restricted to this repository and their protected workflow paths.

Required environment secrets:

- `DEPLOY_SSH_PRIVATE_KEY`
- `DEPLOY_SSH_KNOWN_HOSTS`
- `MAKEPAD_PROXY_PROD_APP_NETWORK`
- `MAKEPAD_PROXY_CANARY_APP_NETWORK`
- `MAKEPAD_PROXY_ALERTECONSO_APP_NETWORK`
- `MAKEPAD_PROXY_LE_PETIT_COIN_APP_NETWORK`
- `MAKEPAD_PROXY_VIF_APP_NETWORK`
- `MAKEPAD_PROXY_MAKEPAD_LANDING_APP_NETWORK`
- `MAKEPAD_PROXY_EVIDELLA_APP_NETWORK`
- `MAKEPAD_PROXY_OPENPANEL_APP_NETWORK`
- `MAKEPAD_PROXY_BRIO_STAGING_APP_NETWORK`
- `MAKEPAD_PROXY_MAILDEV_BRIO_STAGING_WEB_NETWORK`

Required protected `production` environment variables are pinned by both the
workflow and the environment configuration:

- `NGINX_DEPLOY_HOST=135.181.141.31`
- `NGINX_DEPLOY_PORT=22`
- `NGINX_DEPLOY_USER=makepad`
- `NGINX_DEPLOY_REMOTE_DIR=/srv/makepad/nginx`
- `NGINX_DEPLOY_STACK_NAME=makepad-edge`

Keep the SSH key and host-key record in Proton Pass and mirror them only to the
protected GitHub environment. Keep the non-secret target coordinates in the
same Proton Pass server item and GitHub environment variables. The workflow
rejects any target drift before opening an SSH connection.

The workflow deploys only the proxy stack. If the shared application network does not exist yet, it is created on the manager before deployment.
`MAKEPAD_PROXY_RUNTRACE_APP_NETWORK` is optional and defaults to `makepad_runtrace_prod_app`, matching the Runtrace app deployment template; set it only if the Runtrace app stack uses a different overlay network name.

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

For `runtrace.co`, the production proxy expects:

- `/etc/letsencrypt/live/runtrace.co/fullchain.pem`
- `/etc/letsencrypt/live/runtrace.co/privkey.pem`

The `runtrace.co` DNS record must point to the proxy VM before issuing the certificate or deploying the HTTPS route.

For Brio staging, issue certificates only after the two public DNS records point
to the proxy VM:

- `/etc/letsencrypt/live/brio-staging.makepad.fr/{fullchain.pem,privkey.pem}`
- `/etc/letsencrypt/live/maildev-brio-staging.makepad.fr/{fullchain.pem,privkey.pem}`

Do not deploy the Brio routes until both certificates and both encrypted overlay
networks exist. The network secrets must be exactly
`makepad_brio_staging_app` and `makepad_brio_staging_maildev_web`; alternate
values are rejected. The manual workflow fails before updating the shared stack
unless both certificates cover their configured hostnames, remain valid for at
least seven days, chain to the host trust store, and have matching private keys
that Nginx can read. It then waits for the exact pinned Nginx image to converge
and certificate-verifies live Brio and MailDev ingress. The Keycloak host is
owned by the Keycloak ingress stack.
