# Makepad Nginx

Shared Nginx reverse-proxy deployment for Makepad-fr applications. Application
repositories attach their services to the shared external overlay networks;
they do not deploy a second proxy.

## Consent Lens deployment

`consent.makepad.fr` uses `sites/consent-lens-prod.conf.template`, forwarding to `consent-lens-web:3000` on the encrypted, attachable overlay `makepad_consent_lens_prod_app` (`10.0.41.0/24`). `MAKEPAD_PROXY_CONSENT_LENS_APP_NETWORK` can override the shared network name in production Compose and must match the application deployment. The edge overwrites `X-Real-IP`; only this app-specific subnet is trusted by Consent Lens. Request limits, privacy-preserving logs, and unbuffered scan progress are configured in the route.

On the existing VM, use the additive deployment script so unrelated manually installed routes remain intact:

```bash
python3 scripts/deploy-consent-ingress.py --bootstrap --check
python3 scripts/deploy-consent-ingress.py --bootstrap
# Issue TLS with the existing Certbot account and /var/lib/letsencrypt webroot.
python3 scripts/deploy-consent-ingress.py --check
python3 scripts/deploy-consent-ingress.py
```

The script validates a complete candidate copied from the live proxy and preserves all unrelated configs, networks, environment, image, and mounts. TLS files live under `/etc/letsencrypt/live/consent.makepad.fr/`. The existing system Certbot renewal timer reloads the shared proxy.

`scripts/renew-betacrew-cert.sh` preserves the existing Betacrew Certbot directories and renewal schedule, but its deploy hook now validates and reloads the active proxy with `scripts/reload-shared-ingress.sh`. Certificate renewal must not redeploy a stale full proxy stack and remove newer application routes.

## Repository layout

- `compose.yml`: shared Nginx service and virtual-host configuration mounts.
- `envs/production/compose.yml`: production Swarm overrides and external
  network bindings.
- `envs/production/.env.proxy`: public hostnames, upstream aliases, and TLS
  paths.
- `sites/brio-staging.conf.template`: Brio application ingress.
- `sites/maildev-brio-staging.conf.template`: maintainer-only MailDev ingress.
- `sites/00-common.conf.template`: common limits and privacy-safe log formats.

## Brio staging ingress

The shared proxy joins two encrypted overlays for Brio. It validates them but
does not create or relabel them:

- `makepad_brio_staging_app`, owned by the Brio deployment, for the web service.
- `makepad_brio_staging_maildev_web`, owned by the MailDev deployment, for
  MailDev and its GitHub OAuth gate.

The Brio application route forwards to `http://brio-staging-app:8080`. The
MailDev route forwards to `http://maildev-brio-staging:1080` only after an
`auth_request` to `http://maildev-brio-staging-auth:4180`. MailDev relay paths
are denied explicitly even for authenticated maintainers.

Brio ingress removes client-IP forwarding headers, omits URLs, query strings,
IP addresses, referrers, user agents, and request bodies from access logs, and
uses private/no-store caching for MailDev. Both hosts receive HSTS,
clickjacking, MIME-sniffing, referrer, and no-index controls.

The deployment preflight validates both Brio certificates, renders every
existing virtual host, and runs `nginx -t` before changing the live service. It
then waits for Swarm convergence, probes Brio `/livez` for HTTP 204, and checks
that unauthenticated MailDev access redirects to OAuth. A failed release invokes
Swarm rollback and verifies that the exact prior service specification is
healthy again.

## Other application networks

The proxy continues to join the production, canary, Alerte Conso, au petit
coin, Vif, Makepad landing, Evidella, OpenPanel, and Runtrace networks defined
by their `MAKEPAD_PROXY_*_APP_NETWORK` environment values. Brio changes must
not alter those network names or virtual-host behavior.

## Node placement

The proxy service is pinned to nodes carrying:

```sh
docker node update --label-add infra.makepad.proxy=true <proxy-node>
```

## GitHub Actions

CI and deployment use only the existing Makepad self-hosted Linux runner:

```yaml
[self-hosted, Linux, X64, makepad]
```

No repository-specific runner, runner group, JIT runner, custom Checks App, or
launcher App is required. Pull-request CI uses the native `pull_request` event,
rejects forks and drafts before runner allocation, receives no secrets, checks
out the exact event SHA, runs pinned Actionlint and ShellCheck versions, and
exercises both Runtrace and Brio policies in containers.

Deployment is a protected manual workflow. It refuses to run from anything
except `refs/heads/main`, checks out the exact dispatch SHA, and uses the
`production` GitHub environment. Required human review and successful CI are
enforced before changes reach `main`; the deployment workflow does not create a
second release authority.

## Deployment values

The `production` environment requires these secrets:

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
- `MAKEPAD_PROXY_RUNTRACE_APP_NETWORK`
- `MAKEPAD_PROXY_BRIO_STAGING_APP_NETWORK`, exactly
  `makepad_brio_staging_app`
- `MAKEPAD_PROXY_MAILDEV_BRIO_STAGING_WEB_NETWORK`, exactly
  `makepad_brio_staging_maildev_web`

These non-secret deployment coordinates are protected environment variables:

- `NGINX_DEPLOY_HOST`
- `NGINX_DEPLOY_PORT`
- `NGINX_DEPLOY_USER`
- `NGINX_DEPLOY_REMOTE_DIR`
- `NGINX_DEPLOY_STACK_NAME`

The workflow rejects root SSH, uses a pinned known-hosts file, disables ambient
SSH configuration and agents, and keeps SSH material and deployment bundles in
run-scoped temporary directories that are removed in an `always()` step.

Canonical long-lived credentials remain in the existing Proton Pass records
and are mirrored only to the protected GitHub environment. Repository code does
not create, rotate, delete, or move credentials.

Install the Brio release-evidence observer on the proxy host from a protected
`main` checkout before collecting a release package:

```sh
sudo scripts/install-brio-control-receipt.sh
```

The installer atomically publishes the fixed helper at
`/usr/local/libexec/makepad/brio-nginx-control-receipt` as root-owned mode
`0755`. The helper emits canonical `makepad.brio.runtime-controls.v1` JSON. It
reads only the active Swarm service/task, Brio network metadata, and rendered
Nginx configuration; it runs `nginx -t`, makes body-free HTTPS header requests,
and performs verified loopback TLS handshakes with each production SNI name.
It fails closed unless the only public service ports are TCP 80/443, the exact
Brio and MailDev route-policy digests/upstreams are active, both certificate
chains and exact SAN sets are valid with at least seven days remaining, and
the live responses carry the reviewed security and private/no-store headers.
The receipt contains public certificate digests/expiry only. The helper never
opens or emits private-key material, response bodies, cookies, query strings,
client addresses, or upstream payloads; its `nginx -t` subprocess performs the
same certificate/key readability check as deployment. It performs no reload,
rollout, or provider mutation.

### Credential and variable inventory

The exact Proton-to-GitHub map is recorded in
`deploy/credential-inventory.json`. Run the names-only audit with
`./scripts/sync-github-credentials.sh --check --scope production`. The bounded
write mode requires an exact clean protected `main` checkout plus
`--sync --scope production --confirm Makepad-fr/nginx:production`; it must be
run only after explicit operator approval. It streams values through anonymous
pipes and never manages runners, Apps, OAuth resources, or provider policy.
See `docs/credential-sync.md` for the field map and recovery behavior.

## TLS

Certificates must exist on the proxy host under `/etc/letsencrypt`. Brio
requires:

- `/etc/letsencrypt/live/brio-staging.makepad.fr/fullchain.pem`
- `/etc/letsencrypt/live/brio-staging.makepad.fr/privkey.pem`
- `/etc/letsencrypt/live/maildev-brio-staging.makepad.fr/fullchain.pem`
- `/etc/letsencrypt/live/maildev-brio-staging.makepad.fr/privkey.pem`

DNS must point both names to the proxy host before certificate issuance. The
deployment requires each certificate to cover its hostname, chain to the host
trust store, and remain valid for at least seven days.

## Local validation

```sh
actionlint
docker run --rm --network none \
  --volume "$PWD:/workspace:ro" --workdir /workspace \
  docker.io/koalaman/shellcheck:v0.11.0@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d \
  --severity=warning scripts/*.sh
./scripts/test-runtrace-upload-policy.sh
./scripts/test-brio-staging-policy.sh
python3 -m unittest ./tests/test_credential_sync.py
python3 -m unittest ./tests/test_brio_nginx_control_receipt.py
```

## Add Brio staging to the existing ingress

Run `Deploy Brio staging ingress` from main to add Brio and its MailDev OAuth
routes without replacing other projects. This workflow preserves the running
image, environment, mounts, existing configuration objects and network bindings.
It validates the complete candidate configuration beside the current service,
then updates only Brio configuration objects and its two network attachments.
A failed update restores the previous service specification. The same helper
supports `--check` for host-side syntax validation without deployment.

The additive Brio ingress deployment also applies the `nginx -t` health check declared in `compose.yml` to older shared services. It verifies healthy convergence and retains the existing rollback behavior.

### Sentry error monitoring

`sentry.makepad.fr` uses the encrypted attachable `makepad_sentry_prod_app` overlay (`10.0.42.0/24`), configurable through `MAKEPAD_PROXY_SENTRY_APP_NETWORK`. The upstream `makepad-sentry-ingress:80` is the upstream Sentry distribution's internal router on the app VM. Shared Nginx owns public TLS using `/etc/letsencrypt/live/sentry.makepad.fr/`; the existing Certbot renewal hook reloads this proxy. Sentry's own registration is disabled and its dashboard requires sign-in.

Use `python3 scripts/deploy-sentry-ingress.py --check` followed by the same command without `--check` for an additive runtime update preserving all other routes, mounts, environment, image and networks. `--bootstrap` serves only HTTP ACME while provisioning the certificate. This requires the overlay to exist first. Do not deploy a stale full stack over runtime routes created by other projects.
