#!/usr/bin/env python3
"""Add the Pocket Gremlin virtual host to the live edge without replacing other routes."""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import tempfile

os.umask(0o077)
HOST = "pocketgremlin.makepad.fr"
SERVICE = "makepad-edge_nginx"
NETWORK = "makepad_landing_prod_app"
ROOT = Path(__file__).resolve().parents[1]
ROUTE = ROOT / "sites/pocket-gremlin.conf.template"
TARGET = "/etc/nginx/templates/pocket-gremlin.conf.template"
BACKUP_DIR = Path("/var/lib/makepad/pocket-gremlin-deploy")


def run(*args, input_bytes=None):
    return subprocess.check_output(args, input=input_bytes, stderr=subprocess.STDOUT).decode()


def inspect():
    return json.loads(run("docker", "service", "inspect", SERVICE))[0]


def smoke():
    for route, expected in [
        ("/", "Pocket Gremlin — Your iPhone's little inspector"),
        ("/privacy/", "Privacy policy — Pocket Gremlin"),
        ("/support/", "Support — Pocket Gremlin"),
    ]:
        html = run("curl", "-fsS", "--max-time", "10", "--resolve", f"{HOST}:443:135.181.141.31", f"https://{HOST}{route}")
        if expected not in html:
            raise RuntimeError(f"Unexpected response at {route}")


def main():
    if os.geteuid() != 0:
        raise RuntimeError("Run as root on the app proxy host")
    if socket.gethostbyname(HOST) != "135.181.141.31":
        raise RuntimeError("DNS does not point to the app proxy")
    cert = Path(f"/etc/letsencrypt/live/{HOST}/fullchain.pem")
    key = Path(f"/etc/letsencrypt/live/{HOST}/privkey.pem")
    if not cert.is_file() or not key.is_file():
        raise RuntimeError("Pocket Gremlin TLS certificate is missing")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    with open("/var/lock/makepad-pocket-gremlin-ingress.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        before = inspect()
        spec = before["Spec"]["TaskTemplate"]["ContainerSpec"]
        BACKUP_DIR.joinpath("edge-before.json").write_text(json.dumps(before))
        configs = spec.get("Configs", [])
        if any(item["File"]["Name"] == TARGET for item in configs):
            raise RuntimeError("Pocket Gremlin route already present; inspect before updating")
        network = json.loads(run("docker", "network", "inspect", NETWORK))[0]
        if network["Id"] not in {n["Target"] for n in before["Spec"]["TaskTemplate"]["Networks"]}:
            raise RuntimeError("Proxy is not connected to the landing network")
        tasks = run("docker", "ps", "-q", "--filter", f"label=com.docker.swarm.service.name={SERVICE}").split()
        if len(tasks) != 1:
            raise RuntimeError("Expected exactly one running edge task")
        content = ROUTE.read_text()
        with tempfile.TemporaryDirectory(prefix="pocket-gremlin-ingress-") as temp:
            candidate = Path(temp) / "conf"
            candidate.mkdir()
            run("docker", "cp", f"{tasks[0]}:/etc/nginx/conf.d/.", str(candidate))
            candidate.joinpath("pocket-gremlin.conf").write_text(content)
            print(run("docker", "run", "--rm", "--network", f"container:{tasks[0]}", "--volumes-from", f"{tasks[0]}:ro", "--mount", f"type=bind,src={candidate},dst=/etc/nginx/conf.d,readonly", "--entrypoint", "nginx", spec["Image"], "-t"))
        if inspect()["Version"]["Index"] != before["Version"]["Index"]:
            raise RuntimeError("Proxy changed during validation")
        name = "pocket_gremlin_conf_" + hashlib.sha256(content.encode()).hexdigest()[:16]
        if subprocess.run(["docker", "config", "inspect", name], capture_output=True).returncode:
            run("docker", "config", "create", "--label", "com.makepad.owner=Makepad-fr/nginx", name, "-", input_bytes=content.encode())
        try:
            print(run("docker", "service", "update", "--detach=false", "--config-add", f"source={name},target={TARGET},mode=0444", SERVICE))
            after = inspect()
            actual = after["Spec"]["TaskTemplate"]["ContainerSpec"]
            if not {c["ConfigID"] for c in configs} <= {c["ConfigID"] for c in actual["Configs"]}:
                raise RuntimeError("An existing proxy configuration was lost")
            if {n["Target"] for n in before["Spec"]["TaskTemplate"]["Networks"]} != {n["Target"] for n in after["Spec"]["TaskTemplate"]["Networks"]}:
                raise RuntimeError("Proxy networks changed")
            for field in ("Image", "Env", "Mounts", "Command", "Args"):
                if actual.get(field) != spec.get(field):
                    raise RuntimeError(f"Proxy {field} changed")
            active = run("docker", "ps", "-q", "--filter", f"label=com.docker.swarm.service.name={SERVICE}").split()
            if len(active) != 1:
                raise RuntimeError("Proxy has no single healthy task")
            print(run("docker", "exec", active[0], "nginx", "-t"))
            smoke()
            print("Pocket Gremlin HTTPS route active; existing proxy service fields preserved.")
        except BaseException:
            if inspect()["Version"]["Index"] != before["Version"]["Index"]:
                print(run("docker", "service", "rollback", "--detach=false", SERVICE))
            raise


if __name__ == "__main__":
    main()
