#!/usr/bin/env python3
"""Emit a canonical, secret-free receipt for Brio's live Nginx controls."""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import socket
import ssl
import subprocess
from typing import Any


SCHEMA = "makepad.brio.runtime-controls.v1"
SERVICE = "makepad-edge_nginx"
IMAGE = "nginx:1.30-alpine3.24@sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46"
BRIO_HOST = "brio-staging.makepad.fr"
MAILDEV_HOST = "maildev-brio-staging.makepad.fr"
BRIO_UPSTREAM = "http://brio-staging-app:8080"
MAILDEV_UPSTREAM = "http://maildev-brio-staging:1080"
MAILDEV_AUTH_UPSTREAM = "http://maildev-brio-staging-auth:4180"
BRIO_CONFIG_SHA256 = "b03afcc8fd4fcccbe4c07c9c9712124f6ce8c0c03d52c66c55f68cdf8e5eecac"
MAILDEV_CONFIG_SHA256 = "f6ba6c58b75f836dca38a12430b1b15adec574709c2ff12c524bf7b31edd6a1a"
MAX_OUTPUT_BYTES = 512 * 1024
APPLICATION_CSP = "default-src 'self'; base-uri 'none'; object-src 'none'; frame-ancestors 'none'; form-action 'self' https://auth-brio-staging.makepad.fr https://checkout.stripe.com https://billing.stripe.com; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'"
PROXY_PERMISSIONS = "camera=(), geolocation=(), microphone=(), payment=(self), usb=()"
NETWORKS: dict[str, dict[str, str | bool]] = {
    "makepad_brio_staging_app": {
        "internal": False,
        "owner": "Makepad-fr/brio",
        "purpose": "app-edge",
    },
    "makepad_brio_staging_maildev_web": {
        "internal": True,
        "owner": "Makepad-fr/maildev",
        "purpose": "maildev-web",
    },
}


class ReceiptError(RuntimeError):
    """A fail-closed observation error that never includes response content."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ReceiptError(message)


class Runner:
    def run_bytes(self, *arguments: str) -> bytes:
        try:
            result = subprocess.run(
                arguments,
                check=True,
                capture_output=True,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise ReceiptError("Nginx live observation command failed") from error
        require(len(result.stdout) <= MAX_OUTPUT_BYTES, "Nginx observation exceeded its output bound")
        return result.stdout

    def run(self, *arguments: str) -> str:
        try:
            return self.run_bytes(*arguments).decode("utf-8")
        except UnicodeDecodeError as error:
            raise ReceiptError("Nginx observation returned invalid text") from error

    def docker_json(self, *arguments: str) -> Any:
        try:
            return json.loads(self.run("docker", *arguments))
        except json.JSONDecodeError as error:
            raise ReceiptError("Docker returned invalid Nginx observation JSON") from error


def one_container(runner: Runner) -> tuple[str, dict[str, Any]]:
    identifiers = runner.run(
        "docker", "ps", "--quiet", "--filter", f"label=com.docker.swarm.service.name={SERVICE}"
    ).splitlines()
    require(len(identifiers) == 1 and re.fullmatch(r"[a-f0-9]{12,64}", identifiers[0]) is not None,
            "Expected exactly one running shared Nginx task on the proxy host")
    values = runner.docker_json("container", "inspect", identifiers[0])
    require(isinstance(values, list) and len(values) == 1 and isinstance(values[0], dict),
            "Nginx task inspection was not unique")
    return identifiers[0], values[0]


def validate_service(service: dict[str, Any], container: dict[str, Any]) -> None:
    spec = service.get("Spec") or {}
    task = spec.get("TaskTemplate") or {}
    container_spec = task.get("ContainerSpec") or {}
    require(spec.get("Name") == SERVICE, "Observed the wrong Nginx service")
    require(container_spec.get("Image") == IMAGE, "Nginx service image drifted")
    require(((spec.get("Mode") or {}).get("Replicated") or {}).get("Replicas") == 1,
            "Nginx service must have exactly one task")
    ports = ((service.get("Endpoint") or {}).get("Spec") or {}).get("Ports") or []
    normalized_ports = sorted(
        (item.get("Protocol"), item.get("TargetPort"), item.get("PublishedPort"), item.get("PublishMode"))
        for item in ports if isinstance(item, dict)
    )
    require(normalized_ports == [("tcp", 80, 80, "host"), ("tcp", 443, 443, "host")],
            "Nginx public exposure is not exactly TCP 80 and 443")
    config = container.get("Config") or {}
    state = container.get("State") or {}
    labels = config.get("Labels") or {}
    require(config.get("Image") == IMAGE and labels.get("com.docker.swarm.service.name") == SERVICE,
            "Nginx task image or service identity drifted")
    require(state.get("Status") == "running" and (state.get("Health") or {}).get("Status") == "healthy",
            "Nginx task is not running and healthy")


def validate_networks(runner: Runner, service: dict[str, Any], container: dict[str, Any]) -> None:
    targets = {
        item.get("Target")
        for item in (((service.get("Spec") or {}).get("TaskTemplate") or {}).get("Networks") or [])
        if isinstance(item, dict)
    }
    attached = set(((container.get("NetworkSettings") or {}).get("Networks") or {}))
    for name, expected in NETWORKS.items():
        values = runner.docker_json("network", "inspect", name)
        require(isinstance(values, list) and len(values) == 1 and isinstance(values[0], dict),
                f"Nginx Brio network {name} was not unique")
        network = values[0]
        labels = network.get("Labels") or {}
        require(
            network.get("Name") == name
            and network.get("Driver") == "overlay"
            and network.get("Scope") == "swarm"
            and network.get("Attachable") is True
            and network.get("Ingress") is False
            and network.get("Internal") is expected["internal"]
            and (network.get("Options") or {}).get("encrypted") == "true"
            and labels.get("com.makepad.owner") == expected["owner"]
            and labels.get("com.makepad.environment") == "staging"
            and labels.get("com.makepad.instance") == "brio"
            and labels.get("com.makepad.purpose") == expected["purpose"]
            and network.get("Id") in targets
            and name in attached,
            f"Nginx Brio network {name} attachment or isolation drifted",
        )


def validate_rendered_configs(brio: str, maildev: str) -> dict[str, str]:
    digests = {
        "application": hashlib.sha256(brio.encode("utf-8")).hexdigest(),
        "mailCapture": hashlib.sha256(maildev.encode("utf-8")).hexdigest(),
    }
    require(digests == {"application": BRIO_CONFIG_SHA256, "mailCapture": MAILDEV_CONFIG_SHA256},
            "Rendered Brio Nginx route policy drifted")
    for value in (BRIO_HOST, BRIO_UPSTREAM, "location = /applications", "location / {"):
        require(value in brio, "Rendered Brio application route is incomplete")
    for value in (
        MAILDEV_HOST,
        MAILDEV_UPSTREAM,
        MAILDEV_AUTH_UPSTREAM,
        "location = /oauth2/auth",
        "location /oauth2/",
        "location /socket.io/",
        "auth_request /oauth2/auth",
        "return 403;",
    ):
        require(value in maildev, "Rendered Brio MailDev route is incomplete")
    return {name: f"sha256:{value}" for name, value in digests.items()}


def observe_certificate(hostname: str, now: int | None = None) -> dict[str, Any]:
    context = ssl.create_default_context()
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    try:
        with socket.create_connection(("127.0.0.1", 443), timeout=10) as plain:
            with context.wrap_socket(plain, server_hostname=hostname) as secured:
                certificate = secured.getpeercert()
                der = secured.getpeercert(binary_form=True)
                protocol = secured.version()
    except (OSError, ssl.SSLError) as error:
        raise ReceiptError("Live Nginx certificate verification failed") from error
    require(isinstance(certificate, dict) and isinstance(der, bytes) and der,
            "Live Nginx certificate observation was incomplete")
    san_dns = sorted(value.lower() for kind, value in certificate.get("subjectAltName", ()) if kind == "DNS")
    require(san_dns == [hostname], "Live Nginx certificate SAN set drifted")
    not_after = certificate.get("notAfter")
    require(isinstance(not_after, str), "Live Nginx certificate omitted its expiry")
    try:
        expiry = int(ssl.cert_time_to_seconds(not_after))
    except ValueError as error:
        raise ReceiptError("Live Nginx certificate expiry was invalid") from error
    current = int(datetime.datetime.now(datetime.timezone.utc).timestamp()) if now is None else now
    require(expiry >= current + 604800, "Live Nginx certificate expires in less than seven days")
    require(protocol in {"TLSv1.2", "TLSv1.3"}, "Live Nginx negotiated an unapproved TLS version")
    canonical_expiry = datetime.datetime.fromtimestamp(expiry, datetime.timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")
    return {
        "chainVerified": True,
        "hostname": hostname,
        "notAfter": canonical_expiry,
        "sanDNS": san_dns,
        "sha256": f"sha256:{hashlib.sha256(der).hexdigest()}",
    }


def parse_headers(raw: str) -> tuple[int, dict[str, list[str]]]:
    lines = raw.replace("\r\n", "\n").split("\n")
    require(bool(lines) and re.fullmatch(r"HTTP/[0-9.]+ [0-9]{3}(?: .*)?", lines[0]) is not None,
            "Nginx live response status was invalid")
    status = int(lines[0].split()[1])
    headers: dict[str, list[str]] = {}
    for line in lines[1:]:
        if not line:
            break
        require(":" in line, "Nginx live response header was invalid")
        name, value = line.split(":", 1)
        lowered = name.strip().lower()
        require(re.fullmatch(r"[a-z0-9-]+", lowered) is not None, "Nginx live response header name was invalid")
        headers.setdefault(lowered, []).append(value.strip())
    return status, headers


def live_headers(runner: Runner, hostname: str, path: str) -> tuple[int, dict[str, list[str]]]:
    raw = runner.run(
        "curl",
        "--silent",
        "--show-error",
        "--http1.1",
        "--max-time",
        "10",
        "--noproxy",
        "*",
        "--proto",
        "=https",
        "--resolve",
        f"{hostname}:443:127.0.0.1",
        "--dump-header",
        "-",
        "--output",
        "/dev/null",
        f"https://{hostname}{path}",
    )
    return parse_headers(raw)


def require_header(headers: dict[str, list[str]], name: str, value: str) -> None:
    require(value in headers.get(name, []), f"Nginx live {name} header drifted")


def normalize_control_receipt(
    service: dict[str, Any],
    container: dict[str, Any],
    brio_config: str,
    maildev_config: str,
    certificates: dict[str, dict[str, Any]],
    responses: dict[str, tuple[int, dict[str, list[str]]]],
) -> dict[str, Any]:
    validate_service(service, container)
    route_digests = validate_rendered_configs(brio_config, maildev_config)
    require(set(certificates) == {"application", "mailCapture"}, "Nginx certificate receipt set was incomplete")
    for name, hostname in (("application", BRIO_HOST), ("mailCapture", MAILDEV_HOST)):
        certificate = certificates[name]
        require(set(certificate) == {"chainVerified", "hostname", "notAfter", "sanDNS", "sha256"}
                and certificate.get("chainVerified") is True
                and certificate.get("hostname") == hostname
                and certificate.get("sanDNS") == [hostname]
                and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
                                 str(certificate.get("notAfter"))) is not None
                and re.fullmatch(r"sha256:[a-f0-9]{64}", str(certificate.get("sha256"))) is not None,
                "Nginx certificate receipt drifted")
    require(set(responses) == {"application", "mailCapture"}, "Nginx live response set was incomplete")
    app_status, app_headers = responses["application"]
    mail_status, mail_headers = responses["mailCapture"]
    require(app_status == 204, "Brio application live route did not return 204")
    require(mail_status == 302, "MailDev OAuth boundary did not return 302")
    for name, value in {
        "cache-control": "private, no-store",
        "content-security-policy": APPLICATION_CSP,
        "permissions-policy": PROXY_PERMISSIONS,
        "referrer-policy": "strict-origin-when-cross-origin",
        "strict-transport-security": "max-age=31536000",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "x-robots-tag": "noindex, nofollow, noarchive",
    }.items():
        require_header(app_headers, name, value)
    for name, value in {
        "cache-control": "private, no-store",
        "referrer-policy": "no-referrer",
        "strict-transport-security": "max-age=31536000",
        "x-content-type-options": "nosniff",
        "x-frame-options": "DENY",
        "x-robots-tag": "noindex, nofollow, noarchive",
    }.items():
        require_header(mail_headers, name, value)
    redirect_path = f"/oauth2/start?rd=https://{MAILDEV_HOST}/"
    require(mail_headers.get("location") in ([redirect_path], [f"https://{MAILDEV_HOST}{redirect_path}"]),
            "MailDev OAuth redirect target drifted")
    return {
        "controls": {
            "exposure": {"publicTCPPorts": [80, 443]},
            "headers": {
                "application": {
                    "cacheControl": "private, no-store",
                    "contentSecurityPolicy": APPLICATION_CSP,
                    "permissionsPolicy": PROXY_PERMISSIONS,
                    "referrerPolicy": "strict-origin-when-cross-origin",
                    "strictTransportSecurity": "max-age=31536000",
                    "xContentTypeOptions": "nosniff",
                    "xFrameOptions": "DENY",
                    "xRobotsTag": "noindex, nofollow, noarchive",
                },
                "mailCapture": {
                    "cacheControl": "private, no-store",
                    "referrerPolicy": "no-referrer",
                    "strictTransportSecurity": "max-age=31536000",
                    "xContentTypeOptions": "nosniff",
                    "xFrameOptions": "DENY",
                    "xRobotsTag": "noindex, nofollow, noarchive",
                },
            },
            "networks": [
                {
                    "encrypted": True,
                    "internal": expected["internal"],
                    "name": name,
                    "owner": expected["owner"],
                    "purpose": expected["purpose"],
                }
                for name, expected in sorted(NETWORKS.items())
            ],
            "routes": {
                "application": {
                    "hostname": BRIO_HOST,
                    "locations": ["/", "/applications"],
                    "policySHA256": route_digests["application"],
                    "upstream": BRIO_UPSTREAM,
                },
                "mailCapture": {
                    "authUpstream": MAILDEV_AUTH_UPSTREAM,
                    "hostname": MAILDEV_HOST,
                    "locations": ["/", "/oauth2/", "/socket.io/"],
                    "oauthRequired": True,
                    "policySHA256": route_digests["mailCapture"],
                    "relayDenied": True,
                    "upstream": MAILDEV_UPSTREAM,
                },
            },
            "tls": certificates,
        },
        "hostRole": "app",
        "provider": "nginx",
        "schema": SCHEMA,
        "subject": "brio-staging",
    }


def collect_control_receipt(runner: Runner) -> dict[str, Any]:
    services = runner.docker_json("service", "inspect", SERVICE)
    require(isinstance(services, list) and len(services) == 1 and isinstance(services[0], dict),
            "Nginx service inspection was not unique")
    container_id, container = one_container(runner)
    validate_networks(runner, services[0], container)
    runner.run("docker", "exec", container_id, "nginx", "-t")
    brio_config = runner.run("docker", "exec", container_id, "cat", "/etc/nginx/conf.d/brio-staging.conf")
    maildev_config = runner.run(
        "docker", "exec", container_id, "cat", "/etc/nginx/conf.d/maildev-brio-staging.conf"
    )
    certificates = {
        "application": observe_certificate(BRIO_HOST),
        "mailCapture": observe_certificate(MAILDEV_HOST),
    }
    responses = {
        "application": live_headers(runner, BRIO_HOST, "/livez"),
        "mailCapture": live_headers(runner, MAILDEV_HOST, "/"),
    }
    return normalize_control_receipt(
        services[0], container, brio_config, maildev_config, certificates, responses
    )


def main() -> int:
    try:
        receipt = collect_control_receipt(Runner())
    except ReceiptError as error:
        print(str(error), file=os.sys.stderr)
        return 1
    print(json.dumps(receipt, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
