from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import unittest


ROOT = pathlib.Path(os.environ.get("NGINX_CANDIDATE_ROOT", pathlib.Path(__file__).resolve().parents[1])).resolve()
SPEC = importlib.util.spec_from_file_location(
    "brio_nginx_control_receipt",
    ROOT / "scripts" / "brio-nginx-control-receipt.py",
)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader
SPEC.loader.exec_module(MODULE)


def rendered(name: str) -> str:
    values = {}
    for line in (ROOT / "envs" / "production" / ".env.proxy").read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    text = (ROOT / "sites" / name).read_text()
    for key, value in values.items():
        text = text.replace("${" + key + "}", value)
    if "${" in text:
        raise AssertionError(f"unresolved rendered fixture: {name}")
    return text


def service() -> dict:
    return {
        "Spec": {
            "Name": MODULE.SERVICE,
            "Mode": {"Replicated": {"Replicas": 1}},
            "TaskTemplate": {
                "ContainerSpec": {"Image": MODULE.IMAGE},
                "Networks": [{"Target": "app-id"}, {"Target": "mail-id"}, {"Target": "other-id"}],
            },
        },
        "Endpoint": {"Spec": {"Ports": [
            {"Protocol": "tcp", "TargetPort": 443, "PublishedPort": 443, "PublishMode": "host"},
            {"Protocol": "tcp", "TargetPort": 80, "PublishedPort": 80, "PublishMode": "host"},
        ]}},
    }


def container() -> dict:
    return {
        "Config": {
            "Image": MODULE.IMAGE,
            "Labels": {"com.docker.swarm.service.name": MODULE.SERVICE},
        },
        "State": {"Status": "running", "Health": {"Status": "healthy"}},
        "NetworkSettings": {"Networks": {
            "makepad_brio_staging_app": {},
            "makepad_brio_staging_maildev_web": {},
            "another_shared_proxy_network": {},
        }},
    }


def certificates() -> dict:
    return {
        "application": {
            "chainVerified": True,
            "hostname": MODULE.BRIO_HOST,
            "notAfter": "2026-12-04T09:00:00Z",
            "sanDNS": [MODULE.BRIO_HOST],
            "sha256": "sha256:" + "a" * 64,
        },
        "mailCapture": {
            "chainVerified": True,
            "hostname": MODULE.MAILDEV_HOST,
            "notAfter": "2026-12-04T09:00:00Z",
            "sanDNS": [MODULE.MAILDEV_HOST],
            "sha256": "sha256:" + "b" * 64,
        },
    }


def responses() -> dict:
    return {
        "application": (204, {
            "cache-control": ["private, no-store"],
            "content-security-policy": [MODULE.APPLICATION_CSP],
            "permissions-policy": ["camera=(), microphone=(), geolocation=(), payment=()", MODULE.PROXY_PERMISSIONS],
            "referrer-policy": ["strict-origin-when-cross-origin"],
            "strict-transport-security": ["max-age=31536000"],
            "x-content-type-options": ["nosniff"],
            "x-frame-options": ["DENY"],
            "x-robots-tag": ["noindex, nofollow, noarchive"],
        }),
        "mailCapture": (302, {
            "cache-control": ["private, no-store"],
            "location": [f"/oauth2/start?rd=https://{MODULE.MAILDEV_HOST}/"],
            "referrer-policy": ["no-referrer"],
            "strict-transport-security": ["max-age=31536000"],
            "x-content-type-options": ["nosniff"],
            "x-frame-options": ["DENY"],
            "x-robots-tag": ["noindex, nofollow, noarchive"],
        }),
    }


class FakeRunner:
    def __init__(self):
        self.networks = {
            "makepad_brio_staging_app": [{
                "Id": "app-id", "Name": "makepad_brio_staging_app", "Driver": "overlay",
                "Scope": "swarm", "Attachable": True, "Ingress": False, "Internal": False,
                "Options": {"encrypted": "true"},
                "Labels": {
                    "com.makepad.owner": "Makepad-fr/brio",
                    "com.makepad.environment": "staging",
                    "com.makepad.instance": "brio",
                    "com.makepad.purpose": "app-edge",
                },
            }],
            "makepad_brio_staging_maildev_web": [{
                "Id": "mail-id", "Name": "makepad_brio_staging_maildev_web", "Driver": "overlay",
                "Scope": "swarm", "Attachable": True, "Ingress": False, "Internal": True,
                "Options": {"encrypted": "true"},
                "Labels": {
                    "com.makepad.owner": "Makepad-fr/maildev",
                    "com.makepad.environment": "staging",
                    "com.makepad.instance": "brio",
                    "com.makepad.purpose": "maildev-web",
                },
            }],
        }

    def docker_json(self, *arguments: str):
        if arguments[:2] != ("network", "inspect"):
            raise AssertionError(f"unexpected command {arguments!r}")
        return self.networks[arguments[-1]]


class BrioNginxControlReceiptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = service()
        self.container = container()
        self.brio = rendered("brio-staging.conf.template")
        self.maildev = rendered("maildev-brio-staging.conf.template")
        self.certificates = certificates()
        self.responses = responses()

    def receipt(self):
        return MODULE.normalize_control_receipt(
            self.service, self.container, self.brio, self.maildev, self.certificates, self.responses
        )

    def test_canonical_secret_free_receipt(self) -> None:
        receipt = self.receipt()
        self.assertEqual(set(receipt), {"controls", "hostRole", "provider", "schema", "subject"})
        self.assertEqual(receipt["schema"], "makepad.brio.runtime-controls.v1")
        self.assertEqual(receipt["hostRole"], "app")
        self.assertEqual(receipt["provider"], "nginx")
        self.assertEqual(receipt["subject"], "brio-staging")
        self.assertEqual(receipt["controls"]["exposure"], {"publicTCPPorts": [80, 443]})
        self.assertEqual(
            receipt["controls"]["networks"],
            [
                {
                    "encrypted": True,
                    "internal": False,
                    "name": "makepad_brio_staging_app",
                    "owner": "Makepad-fr/brio",
                    "purpose": "app-edge",
                },
                {
                    "encrypted": True,
                    "internal": True,
                    "name": "makepad_brio_staging_maildev_web",
                    "owner": "Makepad-fr/maildev",
                    "purpose": "maildev-web",
                },
            ],
        )
        self.assertEqual(receipt["controls"]["routes"]["application"]["upstream"], MODULE.BRIO_UPSTREAM)
        self.assertEqual(receipt["controls"]["routes"]["mailCapture"]["authUpstream"], MODULE.MAILDEV_AUTH_UPSTREAM)
        serialized = json.dumps(receipt, sort_keys=True, separators=(",", ":"))
        self.assertNotIn("privkey", serialized)
        self.assertNotIn("cookie", serialized.lower())
        self.assertEqual(serialized, json.dumps(json.loads(serialized), sort_keys=True, separators=(",", ":")))

    def test_rendered_route_or_upstream_drift_fails_closed(self) -> None:
        with self.assertRaisesRegex(MODULE.ReceiptError, "route policy drifted"):
            MODULE.validate_rendered_configs(
                self.brio.replace(MODULE.BRIO_UPSTREAM, "http://wrong:8080"), self.maildev
            )

    def test_any_public_port_beyond_80_and_443_fails_closed(self) -> None:
        self.service["Endpoint"]["Spec"]["Ports"].append({
            "Protocol": "tcp", "TargetPort": 8443, "PublishedPort": 8443, "PublishMode": "host",
        })
        with self.assertRaisesRegex(MODULE.ReceiptError, "exactly TCP 80 and 443"):
            self.receipt()

    def test_untrusted_certificate_or_san_drift_fails_closed(self) -> None:
        self.certificates["application"]["chainVerified"] = False
        with self.assertRaisesRegex(MODULE.ReceiptError, "certificate receipt drifted"):
            self.receipt()

    def test_missing_private_cache_or_security_header_fails_closed(self) -> None:
        del self.responses["application"][1]["cache-control"]
        with self.assertRaisesRegex(MODULE.ReceiptError, "cache-control"):
            self.receipt()

    def test_oauth_redirect_accepts_nginx_absolute_normalization_only(self) -> None:
        redirect_path = f"/oauth2/start?rd=https://{MODULE.MAILDEV_HOST}/"
        self.responses["mailCapture"][1]["location"] = [f"https://{MODULE.MAILDEV_HOST}{redirect_path}"]
        self.receipt()
        self.responses["mailCapture"][1]["location"] = [f"https://attacker.example{redirect_path}"]
        with self.assertRaisesRegex(MODULE.ReceiptError, "redirect target"):
            self.receipt()

    def test_exact_encrypted_brio_networks_are_attached(self) -> None:
        runner = FakeRunner()
        MODULE.validate_networks(runner, self.service, self.container)
        runner.networks["makepad_brio_staging_maildev_web"][0]["Internal"] = False
        with self.assertRaisesRegex(MODULE.ReceiptError, "isolation"):
            MODULE.validate_networks(runner, self.service, self.container)

    def test_network_ownership_drift_fails_closed(self) -> None:
        runner = FakeRunner()
        runner.networks["makepad_brio_staging_app"][0]["Labels"]["com.makepad.owner"] = "other"
        with self.assertRaisesRegex(MODULE.ReceiptError, "isolation"):
            MODULE.validate_networks(runner, self.service, self.container)

    def test_header_parser_preserves_duplicate_security_headers(self) -> None:
        status, headers = MODULE.parse_headers(
            "HTTP/1.1 204 No Content\r\nX-Frame-Options: DENY\r\nX-Frame-Options: DENY\r\n\r\n"
        )
        self.assertEqual(status, 204)
        self.assertEqual(headers["x-frame-options"], ["DENY", "DENY"])

    def test_observer_commands_are_read_only(self) -> None:
        source = (ROOT / "scripts" / "brio-nginx-control-receipt.py").read_text()
        for forbidden in (
            "docker\", \"service\", \"update",
            "docker\", \"service\", \"rollback",
            "docker\", \"restart",
            "docker\", \"rm",
            "docker\", \"network\", \"create",
            "curl\", \"--request",
        ):
            self.assertNotIn(forbidden, source)
        self.assertIn('"docker", "exec", container_id, "nginx", "-t"', source)
        self.assertIn('"docker", "exec", container_id, "cat"', source)

    def test_installer_publishes_only_a_root_owned_fixed_helper(self) -> None:
        installer = (ROOT / "scripts" / "install-brio-control-receipt.sh").read_text()
        self.assertIn("root:root:755", installer)
        self.assertIn("brio-nginx-control-receipt", installer)
        self.assertNotIn("sudoers", installer)


if __name__ == "__main__":
    unittest.main()
