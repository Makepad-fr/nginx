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
organization runner group on a dedicated self-hosted runner. CI uses a separate
`Nginx CI` selected-workflow group. There is no persistent runner registered
with the `makepad-nginx-ci-ephemeral` label: a trusted root-only controller
creates one GitHub JIT configuration, boots one self-contained KVM guest, and
destroys the guest after exactly one job. The persistent
`makepad-nginx-ci-attestor` host, JIT hypervisor, and deployment host are three
separate machines and accounts. Both groups allow this public repository but
are restricted to this repository and exact workflow files from
`refs/heads/main`; labels alone are not an authorization boundary.

The `pull_request_target` workflow loads its commands from protected `main`,
checks out the exact same-repository PR head only as candidate data, and runs a
protected-base harness inside the disposable VM. Fork PRs and draft PRs are
rejected. The controller independently authorizes the exact queued workflow,
run attempt, job, base SHA, current PR head, runner group, and exclusive label
set. The protected workflow and JIT runner share a unique
`makepad-nginx-job-<run>-<attempt>` label so a different queued run cannot claim
that VM. Before spawning the launcher, the controller durably records the job
ID plus a 128-bit resource identity that deterministically names its VM,
libvirt network, bridge, nftables table, job directory, qcow2, cloud-init seed,
and GitHub runner. A crash can therefore never turn those objects into
unidentifiable orphans.

The hypervisor uses an immutable root-owned qcow2 approved by SHA-256, copies it
without a backing or data chain, and checks the digest both before and after the
copy. A per-job libvirt network and hypervisor nftables deny private,
WireGuard, link-local, metadata, multicast, IPv6, and hypervisor destinations;
only public DNS and TCP 443 egress remain. The guest contains no repository,
environment, deployment, GitHub App, or Proton Pass credential.

After the Actions job stops, the hypervisor proves the VM, disk, cloud-init
seed, libvirt network, nftables table, and GitHub runner registration absent.
Only then does it bind the authoritative run and attempt result into canonical
JSON, sign it with its Ed25519 key, and dispatch it using the repository-scoped
Launcher App. Cleanup uncertainty emits no result and triggers the independent
host failure alert. The physically separate attestor verifies the immutable
numeric Launcher-App sender ID, signature, freshness, approved image digest,
nonce replay, current PR or protected-main identity, exact runner identity, and
registration absence. A Checks-only App then publishes `policy-and-render`.
Branch protection binds that context to the App ID, so a same-named Actions
check cannot satisfy it.

At controller startup, every durable `launching` entry is reconciled before a
new job is queried or launched. `scripts/reconcile-nginx-ci-jit.sh` first stops
and removes the exact local domain, network, and bridge, preserves the firewall
and disk whenever containment is uncertain, and proves the local objects absent without
depending on DNS or GitHub. A second phase obtains a fresh Launcher App token,
deletes the exact runner registration, and proves it absent. The ledger remains
`launching` across any cleanup failure or another SIGKILL, so systemd retries
the idempotent reconciliation after a full hypervisor reboot. Only complete
proof changes the entry to permanent `failed`; the original job is never
retried, and the controller fails once to activate its independent host alert.

`NGINX_PR_CHECK_APP_PRIVATE_KEY` exists only in the main-restricted
`release-nginx` environment. Repository variables hold the non-secret
`NGINX_PR_CHECK_APP_ID`, `NGINX_CI_LAUNCHER_APP_SENDER_ID`,
`NGINX_CI_APPROVED_BASE_IMAGE_SHA256`, and
`NGINX_CI_ATTESTATION_PUBLIC_KEY`. Manual deployment requires both the exact
successful protected-main `CI` run and its matching App-bound signed-teardown
check before the credentialed job can start.

Bootstrap these controls only from a trusted administrator workstation after
the reviewed files are on `main`:

```sh
./scripts/configure-runner-groups.sh < /secure/path/org-runner-controller-token
./scripts/configure-github-ci-policy.sh \
  NGINX_CHECKS_APP_ID NGINX_LAUNCHER_BOT_ID REVIEWED_BASE_IMAGE_SHA256 \
  /secure/path/hypervisor-attestation-public.pem \
  < /secure/path/repository-admin-token
```

The paths are illustrative. In normal operation stream token and key fields
directly from their canonical Proton Pass items with `pass-cli`; do not print
or leave them in a workspace. Before changing either environment's branch
policy, the repository-policy bootstrap reads its current protection rules and
preserves supported wait timers, exact user/team reviewer IDs, and the
prevent-self-review setting. It does not add reviewers to an unprotected
environment. Duplicate or unknown protection rules, duplicate environment or
branch-policy records, malformed IDs, and truncated API inventories stop the
bootstrap; the final read-back must reproduce the preserved protections and
permit only exact `main`.

The runner reconciler fails closed if the
attestor or deploy host is absent, a persistent runner has the JIT-only label,
any runner exposes unexpected custom labels, or Nginx can reach a repository or
organization runner outside the two selected-workflow groups.

Enable `host/systemd/nginx-ci-queue-controller.service` on the dedicated KVM
hypervisor and install `host/systemd/nginx-ci-queue-alert.service` plus
`host/libexec/send-nginx-host-alert`. Install the launcher, controller, and
`scripts/reconcile-nginx-ci-jit.sh` as immutable root-owned executables. The controller ledger under
`/var/lib/makepad/nginx-ci` is root-owned durable local storage. Its root-only
`/etc/makepad/nginx-ci/controller.env` contains only identifiers, reviewed
digests, intervals, and absolute file paths:

- `NGINX_CI_REPOSITORY_ID`
- `NGINX_CI_LAUNCHER_APP_ID`
- `NGINX_CI_LAUNCHER_APP_INSTALLATION_ID`
- `NGINX_CI_LAUNCHER_APP_PRIVATE_KEY_FILE`
- `NGINX_CI_ATTESTATION_PRIVATE_KEY_FILE`
- `NGINX_CI_CONTROLLER_STATE_DIRECTORY`
- `NGINX_CI_LAUNCHER`
- `NGINX_CI_RECONCILER`
- `NGINX_CI_BASE_IMAGE`
- `NGINX_CI_BASE_IMAGE_SHA256`
- `NGINX_CI_JOB_ROOT`
- `NGINX_CI_LIBVIRT_GROUP`
- `NGINX_CI_PUBLIC_DNS_IPV4`
- `NGINX_CI_POLL_SECONDS`

The App and Ed25519 key files are root-owned mode `0400`. The launcher is a
root-owned mode `0755` regular file reached without a mutable symlink. The base
image and every path component are root-owned and non-writable, and the image
has the filesystem immutable attribute. Treat Docker access inside the guest as
guest root access. The credential-free guest image contains cloud-init, the
GitHub Actions runner under `/opt/actions-runner`, an unprivileged
`actions-runner` account, Docker, Python 3.11+, Node 18+, `actionlint`,
ShellCheck, OpenSSL, curl, and `envsubst`; it is never joined to WireGuard or an
internal application network.

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
- `MAKEPAD_PROXY_RUNTRACE_APP_NETWORK`
- `MAKEPAD_PROXY_BRIO_STAGING_APP_NETWORK`
- `MAKEPAD_PROXY_MAILDEV_BRIO_STAGING_WEB_NETWORK`

Every deployment secret in that inventory is scoped only to the protected
`production` environment. Repository- and organization-level copies are
forbidden: an environment-scoped reference can otherwise silently fall back to
a broader repository secret when an environment field is absent.

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

### Credential and variable inventory

Canonical long-lived values are created or rotated in Proton Pass first. Mirror
only the exact field to its documented GitHub environment or root-only host
boundary; compare stored fingerprints and IDs without printing secret values.
The machine-readable source pins numeric repository ID `1200300778` and every
source/destination tuple in `deploy/credential-inventory.json`. Use
`scripts/sync-github-credentials.sh --check` for a names-only audit. A bounded
sync exact-compares post-write Proton fields and public GitHub variables while
keeping GitHub secret read-back names-only; see `docs/credential-sync.md` for
the trust gates and rollout instructions.

| Proton Pass item | Exact fields | GitHub or host mirror | Authority boundary |
| --- | --- | --- | --- |
| `Nginx · CI Checks App` | `app_id`, `private_key`, `private_key_fingerprint` | Repository variable `NGINX_PR_CHECK_APP_ID`; `release-nginx` secret `NGINX_PR_CHECK_APP_PRIVATE_KEY` | Installed only on `Makepad-fr/nginx`: Metadata read, Checks write, and organization self-hosted-runners read. No Actions, Contents, administration, deployment, environment, or secret access. |
| `Nginx · CI Launcher App` | `app_id`, `installation_id`, `private_key`, `bot_user_id`, `private_key_fingerprint` | Root-only hypervisor private-key file and IDs in `controller.env`; repository variable `NGINX_CI_LAUNCHER_APP_SENDER_ID` receives `bot_user_id` | Installed only on `Makepad-fr/nginx`: Metadata read, Actions read, Pull requests read, Issues write, organization self-hosted-runners write, and Contents write solely for repository dispatch. It is not a branch-protection bypass actor. |
| `Nginx · CI hypervisor attestation` | `ed25519_private_key`, `ed25519_public_key`, `public_key_fingerprint` | Root-owned mode-`0400` hypervisor file; repository variable `NGINX_CI_ATTESTATION_PUBLIC_KEY` | Generate on the dedicated hypervisor. The private key never enters GitHub, a runner guest, or the attestor. |
| `Nginx · CI base image approval` | `qcow2_sha256`, `repository_id` | Repository variable `NGINX_CI_APPROVED_BASE_IMAGE_SHA256`; matching `controller.env` values | Review and hash the self-contained credential-free image before applying its immutable attribute. |
| `Nginx · host control alert webhook` | `url` | Root-owned mode-`0400` file named by `NGINX_HOST_ALERT_URL_FILE` | HTTPS operations channel independent of GitHub; never expose it to a workflow or guest. |
| `Nginx · production deployment` | `private_key`, `known_hosts`, `host`, `port`, `user`, `remote_dir`, `stack_name` | `production` secrets `DEPLOY_SSH_PRIVATE_KEY`, `DEPLOY_SSH_KNOWN_HOSTS`; `NGINX_DEPLOY_*` environment variables | Dedicated non-root deploy account and exact pinned proxy host. |
| `Nginx · production overlay names` | `prod`, `canary`, `alerteconso`, `le_petit_coin`, `vif`, `makepad_landing`, `evidella`, `openpanel`, `runtrace`, `brio_staging`, `maildev_brio_staging_web` | Matching `production` `MAKEPAD_PROXY_*_APP_NETWORK` secrets | Network identifiers only; retain their current secret aliases for workflow compatibility. |
| `Nginx · GitHub repository policy bootstrap` | `repository_admin_token` | Stream once to `configure-github-ci-policy.sh`; never store in Actions | Short-lived repository Administration, Actions, Environments, Variables, and Metadata authority; revoke after read-back. |
| `Nginx · GitHub runner policy bootstrap` | `organization_runner_admin_token` | Stream once to `configure-runner-groups.sh`; never store in Actions | Short-lived organization runner-group authority plus repository Metadata read; revoke after read-back. |

GitHub requires repository `Contents: write` for
`POST /repos/Makepad-fr/nginx/dispatches`; that is the sole reason the Launcher
App holds that permission. All ambient workflow tokens remain explicitly
read-only except the deploy gate's read-only Actions and Checks access.

### Remove the legacy OpenPanel repository secret

The legacy repository-level `MAKEPAD_PROXY_OPENPANEL_APP_NETWORK` must be
removed after, and only after, the canonical Proton value has been explicitly
written to the protected `production` environment. GitHub's API never returns a
secret value, so it cannot compare the two copies. Use this exact sequence from
an approved administrator workstation; neither command prints the value:

```sh
pass-cli item view --vault-name '<operators-vault>' \
  --item-title 'Nginx · production overlay names' --field openpanel | \
  gh secret set MAKEPAD_PROXY_OPENPANEL_APP_NETWORK \
    --repo Makepad-fr/nginx --env production

# Run configure-github-ci-policy.sh exactly as shown in the bootstrap section.
# Its read-back must prove production permits only the main branch.

pass-cli item view --vault-name '<operators-vault>' \
  --item-title 'Nginx · GitHub repository policy bootstrap' \
  --field repository_admin_token | \
  ./scripts/migrate-openpanel-secret-scope.sh \
    --delete-repository-duplicate \
    'Nginx · production overlay names/openpanel'

pass-cli item view --vault-name '<operators-vault>' \
  --item-title 'Nginx · GitHub repository policy bootstrap' \
  --field repository_admin_token | \
  ./scripts/migrate-openpanel-secret-scope.sh --check
```

The CI policy bootstrap narrows both `production` and `release-nginx` to exact
custom `main` before deletion. The migration helper then proves that exact
policy, that the environment copy exists, and that its `updated_at` is later
than the legacy copy. It deletes only the exact repository-level name, re-reads
both inventories, and proves the environment copy remains. Re-run `--check`,
revoke the short-lived administrator token, and record only the Proton item ID,
field name, timestamps, and successful non-secret audit result. Never reverse
the order: deleting the broad copy before re-mirroring from Proton or before
narrowing the environment creates either an outage or a wider credential
boundary.

The workflow deploys only the proxy stack. If the shared application network does not exist yet, it is created on the manager before deployment.
`MAKEPAD_PROXY_RUNTRACE_APP_NETWORK` is required. Keep its canonical value in
the `runtrace` field of `Nginx · production overlay names`; the deployment
workflow has no literal fallback around the protected inventory.

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
that Nginx can read. The preflight renders and validates every existing virtual
host, not only the two Brio additions. It then waits for the exact pinned Nginx
image to converge and certificate-verifies live Brio and MailDev ingress. The
Keycloak host is owned by the Keycloak ingress stack.
