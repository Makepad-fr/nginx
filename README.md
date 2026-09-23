# Makepad Nginx

Shared Nginx reverse-proxy deployment for Makepad-fr applications. Application
repositories attach their services to the shared external overlay networks;
they do not deploy a second proxy.

## Layout

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

## Makepad Scan

Makepad Scan uses scan.makepad.fr and sites/scan.conf.template. Add the scanner application overlay (MAKEPAD_PROXY_SCAN_APP_NETWORK=makepad_scan_app) to the existing proxy and mount only the new virtual host. Validate all existing virtual hosts before reloading; preserve current image, networks and configuration mounts.

## Pluck hostname

Pluck uses `pluck.makepad.fr` (A: `135.181.141.31`) and `sites/pluck.conf.template`. Keep `sites/scan.conf.template` mounted for legacy apps and links. Both hosts proxy to the same protected scanner API without redirects between hosts. Issue the Pluck certificate using the existing `/var/lib/letsencrypt` webroot before adding the TLS virtual host. The existing Certbot renewal hook reloads the shared proxy. Add only the new immutable configuration to the running proxy, preserving its existing image, mounts, networks, and virtual hosts; rollback removes only that configuration. Validate the full live configuration before updating the service.

For the additive Pluck activation, run `python3 scripts/deploy-pluck-ingress.py` on the app VM from this checked-out repository after certificate issuance. It saves a protected pre-update snapshot, validates the full candidate Nginx configuration using the current image and network, detects concurrent service changes, preserves existing configuration IDs and networks, and rolls back a failed update. A second activation fails closed rather than adding duplicate routes.

### Brio private event photos

Brio's authenticated event create/edit routes allow 11 MB request bodies for a
10 MB JPEG/PNG upload plus multipart fields; other routes retain the 1 MB limit.
The application still enforces admin authorization, CSRF, decoded content and
pixel limits.

Only `/brio-staging-event-photos/brio/<64 lowercase hex characters>.jpg` is
proxied over the private network to shared MinIO at `10.80.0.2:9000`. The public
connection uses Brio's existing HTTPS certificate. Preserve the original Host
and SigV4 headers; omit browser cookies and client address headers. All other
paths under the bucket prefix return 404. The bucket remains private and Brio's
service key must permit only GetObject/PutObject/DeleteObject on its `brio/*`
prefix. Anonymous object reads must return 403 before rollout acceptance.

This is a supporting change for Brio native stack #108. Apply through the
existing additive Brio ingress helper after CI and candidate `nginx -t`, retain
the previous service/config references, and verify neighboring routes unchanged.
No global upload-limit increase, new public bucket or storage instance is added.

### Visitaki restricted preview

`envs/production/visitaki-preview.compose.yml` adds `visitaki.com`, its
`www` redirect, and `auth.visitaki.com` to the existing proxy. It is deliberately outside the default
production composition until Visitaki readiness is established. Its
`MAKEPAD_PROXY_VISITAKI_APP_NETWORK` must equal Visitaki's `DEPLOY_APP_NETWORK`.
The apex certificate must cover both apex and www names. Identity requires its own
`/etc/letsencrypt/live/auth.visitaki.com/` certificate and the dedicated
`makepad_keycloak_visitaki_proxy` overlay on the application host. Only Visitaki
realm and theme resources are public; administrative, other-realm, health and
metrics endpoints return 404. Identity access logs are disabled and client IP
headers are cleared. Preserve all existing remote configs and
network attachments when activating this overlay; record the current service
specification before updating, validate `nginx -t`, verify both HTTPS names and
neighboring routes, and roll back the service on a failed check. Do not enable
request access logs: API paths can contain voucher and device values.

The manual `Deploy Visitaki ingress` workflow uses the existing protected
`production` environment and the same deployment concurrency group as the shared
proxy release. Run `bootstrap` first: it adds only HTTP challenge handling and
503 responses for the three Visitaki hosts. Point their DNS at the application
host and issue certificates with the existing Certbot webroot and renewal hook.
Then run `identity` after the dedicated instance is healthy, and `app` after API
compatibility checks pass. Each phase validates the complete live candidate
configuration, preserves unrelated settings and networks, and records the prior
service specification for rollback. Test HTTPS externally and verify neighboring
routes before declaring delivery. The application overlay network is
`makepad_visitaki_preview_app`.
