"""Exercise the shared edge's default HTTP and TLS virtual hosts."""

import http.client
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


def docker(*args):
    return subprocess.run(
        ["docker", *args], check=True, capture_output=True, text=True, timeout=90
    ).stdout.strip()


class UnknownHostIngressTest(unittest.TestCase):
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
            "server { listen 443 ssl; server_name known.example; "
            "ssl_certificate /etc/nginx/test-certs/cert.pem; "
            "ssl_certificate_key /etc/nginx/test-certs/key.pem; "
            "return 200 'known tenant'; }\n"
        )
        container = docker(
            "run", "--detach", "--rm", "--memory", "128m", "--cpus", "0.5",
            "--publish", "127.0.0.1::80", "--publish", "127.0.0.1::443",
            "--volume", f"{COMMON}:/etc/nginx/conf.d/00-common.conf:ro",
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

            # The image's ordinary localhost virtual host must still work.
            connection = http.client.HTTPConnection("127.0.0.1", http_port, timeout=3)
            connection.request("GET", "/", headers={"Host": "localhost"})
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            connection.close()

            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            tls.check_hostname = False
            tls.verify_mode = ssl.CERT_NONE
            with socket.create_connection(("127.0.0.1", https_port), timeout=3) as peer:
                with self.assertRaises((ssl.SSLError, ConnectionResetError, OSError)):
                    with tls.wrap_socket(peer, server_hostname="unrelated.invalid"):
                        self.fail("unknown SNI completed a tenant TLS handshake")

            def tls_status(host):
                with socket.create_connection(("127.0.0.1", https_port), timeout=3) as peer:
                    with tls.wrap_socket(peer, server_hostname="known.example") as connection:
                        connection.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
                        return connection.recv(4096)

            self.assertIn(b" 200 ", tls_status("known.example"))
            self.assertIn(b" 421 ", tls_status("unrelated.invalid"))
        finally:
            subprocess.run(["docker", "rm", "--force", container], capture_output=True, timeout=30)
            fixture.cleanup()


if __name__ == "__main__":
    unittest.main()
