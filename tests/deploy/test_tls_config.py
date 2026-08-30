"""Test configurazione TLS del gateway (WP-E1, GE1)."""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NGINX_CONF = REPO_ROOT / "deploy" / "nginx" / "nginx.conf"
CERT_DIR = REPO_ROOT / "deploy" / "nginx" / "certs"
GEN_CERTS = REPO_ROOT / "scripts" / "gen_certs.sh"


def test_gen_certs_script_exists_and_is_executable() -> None:
    assert GEN_CERTS.exists()
    assert GEN_CERTS.stat().st_mode & 0o111


def test_gen_certs_creates_key_and_cert() -> None:
    subprocess.run(
        [str(GEN_CERTS)],
        check=True,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert (CERT_DIR / "km-engine.key").exists()
    assert (CERT_DIR / "km-engine.crt").exists()


def test_nginx_conf_enables_tls_and_redirect() -> None:
    conf = NGINX_CONF.read_text(encoding="utf-8")
    assert "listen 443 ssl" in conf
    assert "ssl_certificate" in conf
    assert "ssl_certificate_key" in conf
    assert "return 301 https://$host$request_uri" in conf
    assert "Strict-Transport-Security" in conf


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker non disponibile")
def test_nginx_t_syntax() -> None:
    """Valida la sintassi nginx in un container usa-e-getta (non lo stack prod)."""
    proc = subprocess.run(
        [
            "docker", "run", "--rm", "--add-host=km-api:127.0.0.1",
            "-v", f"{NGINX_CONF}:/etc/nginx/nginx.conf:ro",
            "-v", f"{CERT_DIR}:/etc/nginx/certs:ro",
            "nginx:1.27-alpine", "nginx", "-t",
        ],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout
    assert "syntax is ok" in proc.stdout
