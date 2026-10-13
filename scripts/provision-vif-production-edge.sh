#!/usr/bin/env bash
set -euo pipefail
[[ "$(docker info --format '{{.Name}}|{{.Swarm.LocalNodeState}}|{{.Swarm.ControlAvailable}}')" == 'app-server-1|active|true' ]] || { echo 'Expected production app Swarm manager' >&2; exit 1; }
for scope in walking-club platform landing; do
 network="makepad_vif_${scope//-/_}_production_edge"
 if ! docker network inspect "$network" >/dev/null 2>&1; then
  docker network create --driver overlay --attachable --opt encrypted=true --label com.makepad.owner=Makepad-fr/nginx --label "vif.scope=${scope}-production" "$network" >/dev/null
 fi
 docker network inspect "$network" | python3 -c '
import json,sys
n=json.load(sys.stdin)[0]
assert n["Name"]==sys.argv[1] and n["Driver"]=="overlay" and n["Attachable"] and not n["Internal"]
assert n["Options"].get("encrypted")=="true" and n["Labels"].get("com.makepad.owner")=="Makepad-fr/nginx"
assert n["Labels"].get("vif.scope")==sys.argv[2]
' "$network" "${scope}-production"
done
echo 'Vif production edge networks verified; ingress not activated'
