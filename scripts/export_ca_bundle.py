"""Dev-only: export a CA bundle for Docker builds/runs behind a TLS-inspecting proxy.

Usage: uv run python -m scripts.export_ca_bundle   ->  data/cache/ca-bundle.pem (gitignored)

On a network that intercepts HTTPS with a corporate root CA, the container's
public CA list (and uv's) rejects every connection. This writes certifi's
public roots plus the Windows ROOT/CA stores (which include the corporate
root) as one PEM file, used as a BuildKit secret at build time and mounted
with SSL_CERT_FILE at run time (see README "Running behind a TLS-inspecting
proxy"). The grader's normal build and run never need it.
"""

from __future__ import annotations

import ssl
import sys
from pathlib import Path

import certifi

OUT = Path("data/cache/ca-bundle.pem")


def main() -> int:
    if not hasattr(ssl, "enum_certificates"):
        print("Only needed on Windows hosts behind a TLS-inspecting proxy; nothing to do.")
        return 0
    pems = [Path(certifi.where()).read_text(encoding="ascii")]
    seen: set[bytes] = set()
    for store in ("ROOT", "CA"):
        for der, encoding, _trust in ssl.enum_certificates(store):
            if encoding == "x509_asn" and der not in seen:
                seen.add(der)
                pems.append(ssl.DER_cert_to_PEM_cert(der))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text("\n".join(pems), encoding="ascii", newline="\n")
    print(f"wrote {OUT} (certifi + {len(seen)} Windows store certificates)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
