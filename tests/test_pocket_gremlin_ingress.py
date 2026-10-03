from pathlib import Path
import unittest

ROUTE = Path(__file__).resolve().parents[1] / "sites/pocket-gremlin.conf.template"


class PocketGremlinIngressTests(unittest.TestCase):
    def test_dedicated_host_and_certificate(self):
        text = ROUTE.read_text()
        self.assertEqual(text.count("server_name pocketgremlin.makepad.fr;"), 2)
        self.assertIn("/etc/letsencrypt/live/pocketgremlin.makepad.fr/fullchain.pem", text)
        self.assertIn("/etc/letsencrypt/live/pocketgremlin.makepad.fr/privkey.pem", text)
        self.assertIn("location /.well-known/acme-challenge/", text)

    def test_routes_to_existing_landing_service(self):
        text = ROUTE.read_text()
        self.assertIn("rewrite ^(.*)$ /pocket-gremlin$1 break;", text)
        self.assertIn("http://makepad-landing-prod-app:8080", text)
        self.assertLess(text.index("set $pocket_gremlin_upstream"), text.index("rewrite ^(.*)$"))
        self.assertIn("proxy_set_header X-Forwarded-Proto https;", text)


if __name__ == "__main__":
    unittest.main()
