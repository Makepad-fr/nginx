"""Exercise the shared edge's default HTTP and TLS virtual hosts."""

import http.client
import json
import pathlib
import re
import socket
import ssl
import subprocess
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
COMMON = ROOT / "sites/00-common.conf.template"
DEFAULT = ROOT / "sites/default.conf"


def docker(*args):
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=90
    ).stdout.strip()


class UnknownHostIngressTest(unittest.TestCase):
    def test_every_tenant_tls_vhost_rejects_sni_host_mismatch(self):
        guard = "    if ($edge_sni_host_match = 0) { return 421; }"
        for template in (ROOT / "sites").glob("*.conf.template"):
            if template == COMMON:
                continue  # The default TLS vhost rejects the handshake itself.
            source = template.read_text()
            for match in re.finditer(r"(?m)^    listen 443 ssl;\n", source):
                with self.subTest(template=template.name, line=source.count("\n", 0, match.start()) + 1):
                    self.assertTrue(source.startswith(guard, match.end()))

    def test_compose_masks_stock_default_vhost(self):
        rendered = subprocess.run(
            ["docker", "compose", "-f", str(ROOT / "compose.yml"),
             "config", "--format", "json"],
            check=True, capture_output=True, text=True, timeout=30,
        )
        service = json.loads(rendered.stdout)["services"]["nginx"]
        self.assertIn(
            {"source": "default_conf", "target": "/etc/nginx/conf.d/default.conf"},
            service["configs"],
        )

    def test_visitaki_sni_cannot_reach_airloom_or_the_reverse(self):
        compose = (ROOT / "compose.yml").read_text()
        image = re.search(r"^\s+image:\s+(nginx:\S+)", compose, re.MULTILINE)
        self.assertIsNotNone(image)
        with tempfile.TemporaryDirectory() as directory:
            fixture = pathlib.Path(directory)
            certificate = fixture / "cert.pem"
            key = fixture / "key.pem"
            route = fixture / "tenants.conf"
            subprocess.run(
                ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                 "-subj", "/CN=visitaki.com", "-keyout", str(key), "-out", str(certificate)],
                check=True, capture_output=True, timeout=30,
            )
            visitaki = (ROOT / "sites/visitaki-preview.conf.template").read_text()
            airloom = (ROOT / "sites/airloom-prod.conf.template").read_text()
            for source in ("/etc/letsencrypt/live/visitaki.com", "/etc/letsencrypt/live/airloom.makepad.fr"):
                for filename, replacement in (("fullchain.pem", "cert.pem"), ("privkey.pem", "key.pem")):
                    visitaki = visitaki.replace(f"{source}/{filename}", f"/etc/nginx/test-certs/{replacement}")
                    airloom = airloom.replace(f"{source}/{filename}", f"/etc/nginx/test-certs/{replacement}")
            visitaki = visitaki.replace("http://visitaki-preview-api:8080", "http://127.0.0.1:8081")
            airloom = airloom.replace("http://airloom-prod-web:8080", "http://127.0.0.1:8082")
            route.write_text(
                visitaki + airloom
                + "server { listen 8081; return 200 'visitaki tenant'; }\n"
                + "server { listen 8082; return 200 'airloom tenant'; }\n"
            )
            container = docker(
                "run", "--detach", "--rm", "--memory", "128m", "--cpus", "0.5",
                "--publish", "127.0.0.1::443",
                "--volume", f"{COMMON}:/etc/nginx/conf.d/00-common.conf:ro",
                "--volume", f"{DEFAULT}:/etc/nginx/conf.d/default.conf:ro",
                "--volume", f"{route}:/etc/nginx/conf.d/10-tenants.conf:ro",
                "--volume", f"{fixture}:/etc/nginx/test-certs:ro",
                image.group(1),
            )
            try:
                port = int(docker("port", container, "443/tcp").rsplit(":", 1)[1])
                tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                tls.check_hostname = False
                tls.verify_mode = ssl.CERT_NONE

                def response(sni, host):
                    with socket.create_connection(("127.0.0.1", port), timeout=3) as peer:
                        with tls.wrap_socket(peer, server_hostname=sni) as connection:
                            connection.sendall(
                                f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode()
                            )
                            return b"".join(iter(lambda: connection.recv(4096), b""))

                self.assertIn(b"visitaki tenant", response("visitaki.com", "visitaki.com"))
                self.assertIn(b"airloom tenant", response("airloom.makepad.fr", "airloom.makepad.fr"))
                self.assertIn(b" 308 ", response("www.visitaki.com", "www.visitaki.com"))
                self.assertIn(b" 421 ", response("visitaki.com", "airloom.makepad.fr"))
                self.assertIn(b" 421 ", response("airloom.makepad.fr", "visitaki.com"))
            finally:
                subprocess.run(["docker", "rm", "--force", container], capture_output=True, timeout=30)

    def test_unknown_host_cannot_inherit_tenant_route(self):
        compose = (ROOT / "compose.yml").read_text()
        image = re.search(r"^\s+image:\s+(nginx:\S+)", compose, re.MULTILINE)
        self.assertIsNotNone(image)
        fixture = tempfile.TemporaryDirectory()
        fixture_path = pathlib.Path(fixture.name)
        certificate = fixture_path / "cert.pem"
        key = fixture_path / "key.pem"
        route = fixture_path / "known.conf"
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
             "-subj", "/CN=known.example", "-keyout", str(key), "-out", str(certificate)],
            check=True, capture_output=True, timeout=30,
        )
        route.write_text(
            "server { listen 443 ssl; server_name known.example known-www.example; "
            "ssl_certificate /etc/nginx/test-certs/cert.pem; "
            "ssl_certificate_key /etc/nginx/test-certs/key.pem; "
            "if ($edge_sni_host_match = 0) { return 421; } "
            "return 200 'known tenant'; }\n"
            "server { listen 443 ssl; server_name other.example; "
            "ssl_certificate /etc/nginx/test-certs/cert.pem; "
            "ssl_certificate_key /etc/nginx/test-certs/key.pem; "
            "if ($edge_sni_host_match = 0) { return 421; } "
            "return 200 'other tenant'; }\n"
        )
        container = docker(
            "run", "--detach", "--rm", "--memory", "128m", "--cpus", "0.5",
            "--publish", "127.0.0.1::80", "--publish", "127.0.0.1::443",
            "--volume", f"{COMMON}:/etc/nginx/conf.d/00-common.conf:ro",
            "--volume", f"{DEFAULT}:/etc/nginx/conf.d/default.conf:ro",
            "--volume", f"{route}:/etc/nginx/conf.d/10-known.conf:ro",
            "--volume", f"{fixture_path}:/etc/nginx/test-certs:ro",
            image.group(1),
        )
        try:
            http_port = int(docker("port", container, "80/tcp").rsplit(":", 1)[1])
            https_port = int(docker("port", container, "443/tcp").rsplit(":", 1)[1])
            deadline = time.monotonic() + 10
            while True:
                try:
                    connection = http.client.HTTPConnection("127.0.0.1", http_port, timeout=1)
                    connection.request("GET", "/private?token=sample", headers={"Host": "unrelated.invalid"})
                    response = connection.getresponse()
                    self.assertEqual(response.status, 421)
                    self.assertIsNone(response.getheader("Location"))
                    response.read()
                    connection.close()
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(0.1)

            # The image's stock localhost welcome page must not be public.
            connection = http.client.HTTPConnection("127.0.0.1", http_port, timeout=3)
            connection.request("GET", "/", headers={"Host": "localhost"})
            response = connection.getresponse()
            self.assertEqual(response.status, 421)
            response.read()
            connection.close()

            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            tls.check_hostname = False
            tls.verify_mode = ssl.CERT_NONE
            with socket.create_connection(("127.0.0.1", https_port), timeout=3) as peer:
                with self.assertRaises((ssl.SSLError, ConnectionResetError, OSError)):
                    with tls.wrap_socket(peer, server_hostname="unrelated.invalid"):
                        self.fail("unknown SNI completed a tenant TLS handshake")

            def tls_status(sni, host):
                with socket.create_connection(("127.0.0.1", https_port), timeout=3) as peer:
                    with tls.wrap_socket(peer, server_hostname=sni) as connection:
                        connection.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
                        return connection.recv(4096)

            self.assertIn(b" 200 ", tls_status("known.example", "known.example"))
            self.assertIn(b" 200 ", tls_status("known.example", "KNOWN.EXAMPLE"))
            self.assertIn(b" 200 ", tls_status("KNOWN.EXAMPLE", "known.example"))
            self.assertIn(b" 200 ", tls_status("known.example", "known.example:443"))
            self.assertIn(b" 200 ", tls_status("known-www.example", "known-www.example"))
            self.assertIn(b" 200 ", tls_status("other.example", "other.example"))
            self.assertIn(b" 421 ", tls_status("known.example", "unrelated.invalid"))
            self.assertIn(b" 421 ", tls_status("known.example", "other.example"))
            self.assertIn(b" 421 ", tls_status("other.example", "known.example"))
        finally:
            subprocess.run(["docker", "rm", "--force", container], capture_output=True, timeout=30)
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
