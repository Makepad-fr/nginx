# Walking Club staging media credential cutover

Use `https://staging.vif.io` as the application storage endpoint after deploying
the reviewed Vif staging ingress. The retired Brio hostname redirects browser
links and rejects ordinary mutations; it is not a storage endpoint.

The database-bound community and existing object keys keep their immutable
identity. Do not rename the bucket or rewrite historical media references as
part of credential rotation. The edge exposes only the existing opaque JPEG
object path, while MinIO enforces the matching prefix-scoped service policy.
No bucket listing, administrative API or cross-community prefix is exposed.

1. Create a Vif-named service account with the reviewed existing object policy.
   Store its credentials in Proton Pass and the protected GitHub environment.
2. Under the existing staging deployment lock, use `deploy-brio-ingress.py
   --vif-staging --check`, then the same command without `--check`. Capture the
   previous service specification and the resulting receipt. Do not use the
   ordinary shared production release or change other projects' routes.
3. Verify a synthetic object can be written, read byte-for-byte and deleted with
   the new credential through the Vif endpoint. Verify unsigned requests and
   other prefixes remain denied before changing application secret mounts.
4. Replace the application and worker's versioned storage secrets. Verify both
   mounted credentials and actual provider access, then disable the old service
   account and prove it can no longer access an object.
5. Record an administrator upload/read journey on staging and publish the
   assertions, exact build/configuration identifiers and video to Slack.

If the replacement cannot pass the provider or application checks, retain the
old account until recovery succeeds. Restore only this operation's service
specification after checking that no concurrent deployment changed it. Never
weaken TLS verification or broaden the storage policy to make a probe pass.
