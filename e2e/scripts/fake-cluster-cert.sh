#!/usr/bin/env bash
# Mint a throwaway CA and a server certificate for the fake Cohesity cluster on 127.0.0.1.
#
#   fake-cluster-cert.sh <output-dir>
#
# Writes:
#   ca.crt      the CA certificate - public, world-readable. The monitoring configuration points
#               caCertPath here, so the extension does real TLS verification (verifyTls: true)
#               instead of switching it off.
#   server.pem  the server key + certificate in one PEM, which is what the fake server's
#               --certfile expects. Private: mode 0600, owned by whoever runs this; the caller
#               hands it to the service user.
#
# Why a CA and a leaf rather than one self-signed certificate: Python 3.13+ turns on
# VERIFY_X509_STRICT in ssl.create_default_context(), which the extension uses. Strict mode
# rejects a trust anchor without basicConstraints CA:TRUE / keyCertSign, and a leaf without an
# authority key identifier - a bare self-signed server certificate fails verification there
# even though older Pythons accept it.
#
# The SAN is IP:127.0.0.1. An IP literal needs an IPAddress SAN; a DNS SAN of "127.0.0.1" or
# a CN alone does not verify.
set -euo pipefail

out="${1:?usage: fake-cluster-cert.sh <output-dir>}"
mkdir -p "$out"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

cat > "$work/ca.cnf" <<'EOF'
[req]
distinguished_name = dn
prompt = no
x509_extensions = v3_ca
[dn]
CN = cohesity-e2e fake cluster CA
[v3_ca]
basicConstraints = critical, CA:TRUE, pathlen:0
keyUsage = critical, keyCertSign, cRLSign
subjectKeyIdentifier = hash
EOF

cat > "$work/leaf.cnf" <<'EOF'
[req]
distinguished_name = dn
prompt = no
[dn]
CN = 127.0.0.1
[v3_leaf]
basicConstraints = critical, CA:FALSE
keyUsage = critical, digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = IP:127.0.0.1, DNS:localhost
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid, issuer
EOF

openssl req -x509 -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -keyout "$work/ca.key" -out "$work/ca.crt" -days 30 -config "$work/ca.cnf" 2>/dev/null

openssl req -new -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes \
  -keyout "$work/server.key" -out "$work/server.csr" -config "$work/leaf.cnf" 2>/dev/null

openssl x509 -req -in "$work/server.csr" -CA "$work/ca.crt" -CAkey "$work/ca.key" \
  -CAcreateserial -out "$work/server.crt" -days 30 -sha256 \
  -extfile "$work/leaf.cnf" -extensions v3_leaf 2>/dev/null

# The CA key is deleted with $work: nothing can mint another certificate this CA vouches for.
umask 077
cat "$work/server.key" "$work/server.crt" > "$out/server.pem"
chmod 0600 "$out/server.pem"
install -m 0644 "$work/ca.crt" "$out/ca.crt"
echo "wrote $out/ca.crt and $out/server.pem"
