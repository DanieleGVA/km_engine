#!/usr/bin/env bash
# ============================================================================
# gen_certs.sh — certificato TLS self-signed per lo sviluppo (WP-E1, GE1)
#
# Genera una coppia chiave/certificato per il gateway nginx dello stack
# prod-like. SOLO per dev/test: in produzione usare certificati aziendali o
# Let's Encrypt (ADR-003 D2, runbook §7).
#
# Uso:
#   scripts/gen_certs.sh            # genera se assente
#   FORCE=1 scripts/gen_certs.sh    # rigenera sempre
#
# Output:
#   deploy/nginx/certs/km-engine.key
#   deploy/nginx/certs/km-engine.crt
# ============================================================================
set -euo pipefail

CERT_DIR="${CERT_DIR:-deploy/nginx/certs}"
KEY_FILE="$CERT_DIR/km-engine.key"
CRT_FILE="$CERT_DIR/km-engine.crt"
DAYS="${CERT_DAYS:-365}"
CN="${CERT_CN:-localhost}"

mkdir -p "$CERT_DIR"

if [[ -f "$KEY_FILE" && -f "$CRT_FILE" && "${FORCE:-0}" != "1" ]]; then
    echo "Certificati già presenti in $CERT_DIR (usa FORCE=1 per rigenerare)."
    exit 0
fi

echo "Generazione certificato self-signed (CN=$CN, validità=${DAYS}g)..."
openssl req -x509 -newkey rsa:2048 -nodes \
    -keyout "$KEY_FILE" \
    -out "$CRT_FILE" \
    -days "$DAYS" \
    -subj "/CN=$CN" \
    -addext "subjectAltName=DNS:localhost,DNS:$CN,IP:127.0.0.1" \
    -addext "keyUsage=digitalSignature,keyEncipherment" \
    -addext "extendedKeyUsage=serverAuth"

chmod 600 "$KEY_FILE"
chmod 644 "$CRT_FILE"
echo "OK: $CRT_FILE"
echo "OK: $KEY_FILE"
