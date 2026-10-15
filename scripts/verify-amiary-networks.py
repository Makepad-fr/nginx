#!/usr/bin/env python3
"""Read-only prerequisite check for the optional Amiary ingress overlay."""
import json
import os
import subprocess

def validate(network, name):
    if network.get("Name") != name or network.get("Driver") != "overlay" or network.get("Scope") != "swarm":
        raise ValueError("Amiary requires the exact reviewed Swarm overlay")
    if "encrypted" not in (network.get("Options") or {}):
        raise ValueError("Amiary overlay must be encrypted")
    if not network.get("Attachable"):
        raise ValueError("Amiary overlay must be attachable")

def main():
    names=[os.environ["MAKEPAD_PROXY_AMIARY_"+kind+"_APP_NETWORK"] for kind in ("PROD","CANARY")]
    if len(set(names)) != 2 or not all(names):
        raise ValueError("Production and canary must use different named overlays")
    for name in names:
        data=json.loads(subprocess.check_output(["docker","network","inspect",name]))
        if len(data)!=1:raise ValueError("Expected one exact network")
        validate(data[0],name)
    print("Amiary production and canary encrypted overlays verified")
if __name__=="__main__":main()
