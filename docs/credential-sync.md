# Nginx credential inventory and bounded GitHub sync

`deploy/credential-inventory.json` is the reviewed, machine-readable map from
the shared Proton Pass vault `Makepad` to the GitHub and root-host boundaries
used by numeric repository `1200300778` (`Makepad-fr/nginx`). The helper also
contains the complete expected tuple set, so a syntactically valid inventory
edit cannot silently redirect a destination to another item or field. The
inventory does not contain values. Repository code never creates, rotates,
exports, or deletes a provider credential.

Run this helper only from a clean checkout of reviewed protected code on a
trusted administrator workstation. Never run it from a pull-request checkout
or a self-hosted Actions workspace.

## Names-only audit

The default mode audits the whole inventory:

```sh
./scripts/sync-github-credentials.sh --check
```

An audit can also be bounded to one scope:

```sh
./scripts/sync-github-credentials.sh --check --scope production
./scripts/sync-github-credentials.sh --check --scope release-nginx
./scripts/sync-github-credentials.sh --check --scope repository-variables
./scripts/sync-github-credentials.sh --check --scope host-boundaries
```

Check mode calls `pass-cli item list` for active item titles. It never calls
`pass-cli item view`; therefore it cannot read a Proton field value. It asks
GitHub CLI to emit only destination names and reads public repository and
environment-protection metadata. It also verifies protected `main` and the
Actions workflow-token policy. Output is limited to names, classifications,
policy state, and counts. A host destination is reported as
`operator-managed`; the helper never connects to that host.

An item title must occur exactly once. A missing or duplicated title, missing
required destination, unreadable inventory, repository identity mismatch, or
environment-policy mismatch fails with status `1`. An unexpected broad,
legacy, or unmanaged GitHub name fails with status `2`. A successful names-only
audit does not prove field contents; field validation happens only in an
explicit sync.

## One-scope sync

Every mutation requires one exact writable scope:

```sh
./scripts/sync-github-credentials.sh --sync --scope production
./scripts/sync-github-credentials.sh --sync --scope release-nginx
./scripts/sync-github-credentials.sh --sync --scope repository-variables
```

`host-boundaries` is deliberately audit-only. The helper rejects an omitted,
unknown, repeated, or host-only write scope.

Before the first GitHub write, sync mode:

1. validates the strict inventory schema, the complete immutable tuple map,
   and the exact vault name;
2. pins the repository to numeric ID `1200300778`, exact public visibility,
   `fork: false`, and default branch `main`;
3. requires protected `main` to have strict App-bound `policy-and-render`, one
   approving review with the configured review safeguards, signed commits,
   administrator enforcement, linear history, conversation resolution, and
   no force pushes or deletion;
4. requires the Actions workflow token to default to read-only and forbids it
   from approving pull requests;
5. rejects every repository Actions secret and every unlisted destination;
6. binds each selected environment, its protection rules, and its sole exact
   `main` deployment branch policy to their numeric IDs for the whole run;
7. proves each selected Proton item title is unique; and
8. reads every selected field through an anonymous pipe into a size/NUL/empty
   validator. The same validator enforces the pinned deployment coordinates,
   canonical Brio network names, Docker-name syntax, positive App IDs,
   lowercase image digest, Ed25519 public-key envelope, and expected
   private-key/known-hosts structure. It retains only a per-run keyed SHA-256
   fingerprint for subsequent exact comparison. The Checks App source ID must
   equal the App ID already bound to protected `main`.

It then re-reads the selected Proton item titles plus GitHub identity, names,
protected-main policy, Actions policy, resource IDs, and the complete preserved
environment-protection snapshot. Only after those checks pass does it read each
Proton field a second time. A private in-process validator exact-compares the
keyed fingerprint before it starts `gh`, then supplies the already validated
bytes to `gh secret set` or `gh variable set` through standard input. A value
never enters a command argument, exported environment variable, shell variable,
log, dotenv file, or temporary file. Shell tracing and CLI debug output are
disabled, the temporary directory is owner-only and contains only metadata and
per-run keyed fingerprints, and process core dumps are disabled before field
reads.

After all writes, the helper re-reads every selected Proton item and field and
exact-compares it with the preflight fingerprint. The final GitHub read-back
must reproduce the same numeric identities, protected-main and Actions policy,
environment settings, exact branch-policy IDs, and complete destination name
sets. Every public variable value is exact-compared with its Proton source;
secret read-back remains names-only because GitHub never returns secret values.
The helper never creates or edits an environment policy and never deletes a
GitHub name. Resolve a reported legacy name through a separately reviewed,
exact-name migration such as `migrate-openpanel-secret-scope.sh`.

GitHub does not offer a transaction spanning multiple secrets or variables. A
provider failure after the first accepted field can leave an incomplete but
bounded update. The helper stops immediately, reports only the failed
destination name, and performs no rollback or deletion. Re-audit, resolve the
provider failure, and idempotently repeat the same scope.

## GitHub mirrors

Every arrow below means `Proton item/field -> GitHub destination`.

### Protected `production` environment

| Proton item and field | Environment kind and destination |
| --- | --- |
| `Nginx · production deployment/private_key` | secret `DEPLOY_SSH_PRIVATE_KEY` |
| `Nginx · production deployment/known_hosts` | secret `DEPLOY_SSH_KNOWN_HOSTS` |
| `Nginx · production deployment/host` | variable `NGINX_DEPLOY_HOST` |
| `Nginx · production deployment/port` | variable `NGINX_DEPLOY_PORT` |
| `Nginx · production deployment/user` | variable `NGINX_DEPLOY_USER` |
| `Nginx · production deployment/remote_dir` | variable `NGINX_DEPLOY_REMOTE_DIR` |
| `Nginx · production deployment/stack_name` | variable `NGINX_DEPLOY_STACK_NAME` |

The `Nginx · production overlay names` item supplies environment secrets:

| Proton field | GitHub destination |
| --- | --- |
| `prod` | `MAKEPAD_PROXY_PROD_APP_NETWORK` |
| `canary` | `MAKEPAD_PROXY_CANARY_APP_NETWORK` |
| `alerteconso` | `MAKEPAD_PROXY_ALERTECONSO_APP_NETWORK` |
| `le_petit_coin` | `MAKEPAD_PROXY_LE_PETIT_COIN_APP_NETWORK` |
| `vif` | `MAKEPAD_PROXY_VIF_APP_NETWORK` |
| `makepad_landing` | `MAKEPAD_PROXY_MAKEPAD_LANDING_APP_NETWORK` |
| `evidella` | `MAKEPAD_PROXY_EVIDELLA_APP_NETWORK` |
| `openpanel` | `MAKEPAD_PROXY_OPENPANEL_APP_NETWORK` |
| `runtrace` | `MAKEPAD_PROXY_RUNTRACE_APP_NETWORK` |
| `brio_staging` | `MAKEPAD_PROXY_BRIO_STAGING_APP_NETWORK` |
| `maildev_brio_staging_web` | `MAKEPAD_PROXY_MAILDEV_BRIO_STAGING_WEB_NETWORK` |

Network identifiers retain their existing secret aliases for workflow
compatibility. No repository- or organization-level copy is permitted because
an absent environment secret can otherwise fall back to that broader scope.

### Protected `release-nginx` environment

| Proton item and field | Environment destination |
| --- | --- |
| `Nginx · CI Checks App/private_key` | secret `NGINX_PR_CHECK_APP_PRIVATE_KEY` |

The physically separate attestor receives only this Checks-only App key. It
does not receive the Launcher App key, teardown private key, deployment key, or
base-image file.

### Repository variables

These are public or non-secret integrity anchors needed before a job can enter
an environment. They are still streamed on standard input, then read back by
exact name and value without printing the value.

| Proton item and field | Repository variable |
| --- | --- |
| `Nginx · CI Checks App/app_id` | `NGINX_PR_CHECK_APP_ID` |
| `Nginx · CI Launcher App/bot_user_id` | `NGINX_CI_LAUNCHER_APP_SENDER_ID` |
| `Nginx · CI base image approval/qcow2_sha256` | `NGINX_CI_APPROVED_BASE_IMAGE_SHA256` |
| `Nginx · CI hypervisor attestation/ed25519_public_key` | `NGINX_CI_ATTESTATION_PUBLIC_KEY` |

The policy configurator remains the semantic authority for validating the App
IDs, bot identity, SHA-256 format, Ed25519 key, runner groups, and App
permissions. The generic sync helper mirrors bytes but does not weaken those
checks.

## Root-only and operator-only boundaries

These values stay canonical in Proton but must never become Actions secrets or
variables:

| Proton item and field | Root/operator destination |
| --- | --- |
| `Nginx · CI Checks App/private_key_fingerprint` | operator verification record |
| `Nginx · CI Launcher App/app_id` | root `controller.env:NGINX_CI_LAUNCHER_APP_ID` |
| `Nginx · CI Launcher App/installation_id` | root `controller.env:NGINX_CI_LAUNCHER_APP_INSTALLATION_ID` |
| `Nginx · CI Launcher App/private_key` | root-only `launcher-app-private-key.pem`, mode `0400` |
| `Nginx · CI Launcher App/private_key_fingerprint` | operator verification record |
| `Nginx · CI hypervisor attestation/ed25519_private_key` | root-only `attestation-private-key.pem`, mode `0400` |
| `Nginx · CI hypervisor attestation/public_key_fingerprint` | operator verification record |
| `Nginx · CI base image approval/qcow2_sha256` | root `controller.env:NGINX_CI_BASE_IMAGE_SHA256` |
| `Nginx · CI base image approval/repository_id` | root `controller.env:NGINX_CI_REPOSITORY_ID` |
| `Nginx · host control alert webhook/url` | root file named by `NGINX_HOST_ALERT_URL_FILE`, mode `0400` |
| `Nginx · GitHub repository policy bootstrap/repository_admin_token` | trusted operator stdin only |
| `Nginx · GitHub runner policy bootstrap/organization_runner_admin_token` | trusted operator stdin only |

Install a root-only key or bearer URL from a trusted console through one pipe
into a newly created owner-only file, then compare its Proton fingerprint. Do
not use argv, shell history, a clipboard, dotenv file, runner workspace, or
Actions secret as an intermediate. Bootstrap tokens are short-lived, streamed
to their exact policy script, verified by read-back, and revoked.

Ephemeral workflow tokens, GitHub App installation tokens, JIT registration
tokens, and per-job nonces are intentionally absent from the inventory. They
are minted for one operation and must never be retained.

## Rollout gate

Preserve the staged rollout:

1. merge and independently review the Stage 1 repository controls;
2. create or rotate canonical Proton fields and record non-secret fingerprints;
3. reconcile runner groups, Apps, and exact-main environments from protected
   code, then run the whole names-only audit;
4. sync one GitHub scope at a time and repeat the audit after every scope;
5. remove a legacy duplicate only through its separately reviewed migration;
6. enable the Stage 2 workflow/deployment boundary only after all read-backs
   and independent host checks pass.

No step in this repository change performs those operational mutations.
