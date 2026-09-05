# Nginx production credential sync

`deploy/credential-inventory.json` is the reviewed map from the shared Proton
Pass vault `Makepad` to the protected GitHub `production` environment for
numeric repository `1200300778` (`Makepad-fr/nginx`). It contains names and
destinations, never credential values.

The inventory covers the two SSH secrets, every network name consumed by the
deployment workflow, and the five non-secret deployment coordinates. It does
not contain runner credentials, GitHub App credentials, OAuth credentials, or
a second release environment. Nginx CI and deployment use the existing Makepad
self-hosted runner.

Run the names-only audit from a trusted administrator checkout:

```sh
./scripts/sync-github-credentials.sh --check --scope production
```

Check mode verifies:

- the exact repository and production-environment numeric identities;
- the sole `main` deployment-branch policy;
- protected `main`, including the GitHub Actions check, code-owner approval,
  signed commits, administrator enforcement, and force-push/deletion denial;
- unique active Proton item titles without reading any Proton field value;
- presence of every managed environment destination; and
- absence of a broader repository-level copy of each managed name.

Unrelated Nginx environment destinations are outside this Brio sync and are
left unchanged. The helper never deletes, moves, creates, or rotates a Proton
item, GitHub environment, policy, runner, App, or OAuth resource.

After independent review and explicit operator approval, sync the single
bounded scope from the exact clean protected `main` commit:

```sh
./scripts/sync-github-credentials.sh \
  --sync \
  --scope production \
  --confirm Makepad-fr/nginx:production
```

Before the first write, the helper validates all Proton values and stores only
per-run keyed HMACs. Values pass through anonymous pipes directly from
`pass-cli item view` to an in-process validator and then to `gh secret set` or
`gh variable set`; they never enter argv, a shell variable, an exported
environment variable, a log, or a temporary value file. Shell tracing and core
dumps are disabled before values are read.

The helper rechecks Proton item identity and GitHub repository, environment,
branch, and protection state before and after writes. It re-reads every Proton
field after the write and exact-compares each public GitHub variable. GitHub
does not expose stored secret values, so secret read-back proves exact names,
not plaintext equality.

GitHub offers no transaction across multiple destinations. A provider failure
can therefore leave a partial, bounded update. The helper stops on the first
failure without deleting or rolling back another value. Resolve the error,
repeat the names-only audit, and rerun the same idempotent production sync.

## Proton fields

`Nginx · production deployment` contains:

- `private_key`
- `known_hosts`
- `host` (`135.181.141.31`)
- `port` (`22`)
- `user` (`makepad`)
- `remote_dir` (`/srv/makepad/nginx`)
- `stack_name` (`makepad-edge`)

`Nginx · production overlay names` contains:

- `prod`
- `canary`
- `alerteconso`
- `le_petit_coin`
- `vif`
- `makepad_landing`
- `evidella`
- `openpanel`
- `runtrace`
- `brio_staging` (`makepad_brio_staging_app`)
- `maildev_brio_staging_web` (`makepad_brio_staging_maildev_web`)

Network fields remain environment secrets for compatibility with the existing
workflow. Deployment coordinates are environment variables and are
exact-compared after sync.
