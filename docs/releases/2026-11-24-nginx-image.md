# Shared proxy image release — 24 November 2026

The candidate updates only the official Nginx image digest. Deploy it with
`scripts/upgrade-shared-image.py`, preserving the complete live service spec.

- Predecessor: `nginx:1.31-alpine3.24@sha256:785835ea65db4bba6f021acc0f3be91b0fef76a53fe41f8588c6698d2aed0606`.
- Candidate: `nginx:1.31-alpine3.24@sha256:fb4658584bd9a916ffb6f6e365f97c25cfdb2923801f551ea08356e695f14ff7`.
- Source PR: #100 at `16a06689b57ec20bcd69f0fad98dbd2c14628838`.
- Independent repository tests: 79 passed.
- Exact source CI: [CI 44687266279](https://github.com/Makepad-fr/nginx/actions/runs/44687266279) and [proxy CI 44687266194](https://github.com/Makepad-fr/nginx/actions/runs/44687266194) passed.

The live-host `--check` preflight passed with the candidate image against all
rendered routes, certificates, existing mounts and upstreams. It pulled and
validated the immutable candidate without updating the service. The live
predecessor was `makepad-edge_nginx` at service version `105402`, with the previous
update completed and automatic health-failure rollback configured.

The baseline records 33 HTTPS responses across the 30 explicit public hostnames
and Visitaki health (204), protected readiness (403), and identity discovery
(200). Existing root-route 404/421 responses and authentication redirects are
baseline behavior. Compare status and redirect destination after deployment;
these root routes are not application-specific health checks.

After the native stack merges, require successful CI at the merged main SHA and
recheck the expected live image before invoking the helper without `--check`.
The target is only the shared proxy image; all routes, mounts, networks, runtime
settings and neighboring services must stay unchanged. Verify the helper's exact
service-spec comparison, healthy task, `nginx -t`, unchanged neighboring container
identities/images/start times, and all 33 endpoint responses. Retain its sanitized
receipt and deployment image identity.

Swarm owns automatic health-failure rollback. The predecessor above remains the
rollback target; inspect any failed rollout before further mutation. The deployment below completed without invoking rollback.

## Verified deployment

PR #100 merged through native stack #102 as
`470e10b47061f0be6a85d24646a213e45d393084`. Both exact-main checks passed:
[CI 45288283748](https://github.com/Makepad-fr/nginx/actions/runs/45288283748)
and [proxy CI 45288283671](https://github.com/Makepad-fr/nginx/actions/runs/45288283671).

The image-only helper then converged `makepad-edge_nginx` at service version
`105715`, with the candidate image healthy and `nginx -t` passing. The receipt
recorded predecessor-spec SHA-256
`4f11d2b10c196045fc1d78e67fc2131109a87c65a8a7b3eba7a3a3221e582b14`.
Independent before/after inspection confirmed the complete service specification
apart from the requested image was unchanged (normalized SHA-256
`ce97e538e2e8ba005291e174cd80a2be387bdbffcf8b045e258d36ef5e1a4aad`).
All 115 neighboring container identities, images, start times and restart counts
remained unchanged. All 33 HTTPS status codes and redirect destinations matched
the fresh baseline, including Visitaki health, protected readiness and identity.
