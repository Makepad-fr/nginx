#!/usr/bin/env python3
"""Exercise the VIF proxy with a real upstream response containing a large cookie."""
import pathlib
import re
import subprocess
import tempfile
import time
import uuid

ROOT = pathlib.Path(__file__).resolve().parents[1]
image = re.search(r"image:\s*(nginx:\S+@sha256:\S+)", (ROOT / "compose.yml").read_text())[1]
source = (ROOT / "sites/vif-prod.conf.template").read_text()
location = source.rsplit("    location / {", 1)[1].split("    }", 1)[0]
location = location.replace("${VIF_PROD_UPSTREAM}", "http://127.0.0.1:8081")


def exercise(block, expected):
    name = "vif-header-review-" + uuid.uuid4().hex[:12]
    config = """events {}\nhttp {
      map $http_upgrade $connection_upgrade { default upgrade; '' close; }
      server { listen 8081; location / {
        COOKIES
        return 200 "upstream-ok";
      } }
      server { listen 8080; location / { BLOCK } }
    }""".replace("COOKIES", "\n".join('add_header Set-Cookie "session%d=%s; HttpOnly; Secure; SameSite=Lax";' % (i, "x" * 3000) for i in range(8))).replace("BLOCK", block)
    # Only these disposable containers and files are touched; no host ports or networks.
    with tempfile.TemporaryDirectory(prefix="vif-header-review-") as directory:
        path = pathlib.Path(directory) / "nginx.conf"
        path.write_text(config)
        try:
            subprocess.run(["docker", "run", "-d", "--name", name, "--network", "none",
                            "-v", f"{path}:/etc/nginx/nginx.conf:ro", image],
                           check=True, stdout=subprocess.DEVNULL)
            for _ in range(50):
                result = subprocess.run(["docker", "exec", name, "wget", "-S", "-O", "-",
                                         "http://127.0.0.1:8080/oidc/callback"],
                                        capture_output=True, text=True)
                if "HTTP/1.1" in result.stderr:
                    break
                time.sleep(0.1)
            assert f"HTTP/1.1 {expected}" in result.stderr, "Unexpected proxy status"
            if expected == 200:
                assert result.returncode == 0 and result.stdout == "upstream-ok"
                assert all("session%d=" % i + "x" * 3000 in result.stderr for i in range(8)), "Cookie was truncated"
        finally:
            subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=False)


baseline = re.sub(r"\s*proxy_(?:buffer_size|buffers|busy_buffers_size)\s+[^;]+;", "", location)
exercise(baseline, 502)
exercise(location, 200)
print("Default buffers reject the large response; VIF buffers preserve the full cookie and body.")
