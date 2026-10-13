#!/usr/bin/env bash
set -euo pipefail
# Shared ingress owns this overlay; application containers only attach to it.
[[ "$(docker info --format '{{.Name}}|{{.Swarm.LocalNodeState}}|{{.Swarm.ControlAvailable}}')" == 'app-server-1|active|true' ]] || {
  echo 'Expected the existing app-host Swarm manager' >&2; exit 1;
}
network=makepad_vif_landing_staging_edge
if ! docker network inspect "$network" >/dev/null 2>&1; then
  docker network create --driver overlay --attachable --opt encrypted=true \
    --label com.makepad.owner=Makepad-fr/nginx --label vif.scope=landing-staging "$network" >/dev/null
fi
docker network inspect "$network" | python3 -c '
import json,sys
n=json.load(sys.stdin)[0]
assert n["Name"]=="makepad_vif_landing_staging_edge"
assert n["Driver"]=="overlay" and n["Attachable"] and not n["Internal"]
assert n["Options"].get("encrypted")=="true"
assert n["Labels"].get("com.makepad.owner")=="Makepad-fr/nginx"
assert n["Labels"].get("vif.scope")=="landing-staging"
print("Dedicated encrypted Vif landing edge overlay verified")'
