"""Release signing for Fh_Report (maintainer tool, not part of the plugin install).

`/fh_report upgrade` only installs a release whose zip carries a valid Ed25519 signature
made with YOUR private key; the matching public key is stored in the plugin
(UPGRADE_PUBLIC_KEY in plugins/fh_report/commands.py).

  1. Once:        python tools/release_tool.py keygen --private C:\\keys\\fh_report_release.key
                  -> prints the public key: paste it into UPGRADE_PUBLIC_KEY and commit it.
                  Keep the private key file OUTSIDE this repository and backed up: whoever holds
                  it can publish releases the plugin will install; if it is lost, a release with
                  a new public key has to be installed by hand (install.cmd).
  2. Each release: bump FH_REPORT_RELEASE, then
                  python tools/release_tool.py build --private C:\\keys\\fh_report_release.key
                  -> dist/fh_report-<version>.zip and dist/fh_report-<version>.zip.sig
                  Attach BOTH files to the GitHub release (the tag is v.<version>).
  3. Anyone can check a pair:  python tools/release_tool.py verify dist/fh_report-14.5.0.zip
"""
from __future__ import annotations

import argparse
import base64
import io
import os
import re
import sys
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILES = ("plugins/fh_report/__init__.py", "plugins/fh_report/commands.py",
         "plugins/fh_report/listener.py", "plugins/fh_report/version.py", "migrate_config.py")
_ZIP_TIME = (2020, 1, 1, 0, 0, 0)          # fixed, so the same files always give the same zip


def release_version(root: str = ROOT) -> str:
    with open(os.path.join(root, "plugins", "fh_report", "commands.py"), encoding="utf-8") as f:
        m = re.search(r'^FH_REPORT_RELEASE\s*=\s*"(\d+\.\d+\.\d+)"', f.read(), re.MULTILINE)
    if not m:
        sys.exit("FH_REPORT_RELEASE not found in commands.py")
    return m.group(1)


def build_zip(root: str = ROOT) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name in FILES:
            with open(os.path.join(root, *name.split("/")), "rb") as f:
                info = zipfile.ZipInfo(name, _ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                z.writestr(info, f.read())
    return buf.getvalue()


def public_b64(key: Ed25519PrivateKey) -> str:
    raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return base64.b64encode(raw).decode()


def load_private(path: str) -> Ed25519PrivateKey:
    with open(path, encoding="ascii") as f:
        return Ed25519PrivateKey.from_private_bytes(base64.b64decode(f.read().strip()))


def cmd_keygen(args) -> None:
    path = os.path.abspath(args.private)
    if os.path.commonpath([path, ROOT]) == ROOT:
        sys.exit("Refusing to write the private key inside the repository: choose a folder outside it.")
    if os.path.exists(path):
        sys.exit(f"{path} already exists: not overwriting it.")
    key = Ed25519PrivateKey.generate()
    raw = key.private_bytes(serialization.Encoding.Raw, serialization.PrivateFormat.Raw,
                            serialization.NoEncryption())
    with open(path, "w", encoding="ascii") as f:
        f.write(base64.b64encode(raw).decode() + "\n")
    print(f"Private key written to {path}  (keep it secret and back it up)")
    print(f"Public key (paste into UPGRADE_PUBLIC_KEY):\n{public_b64(key)}")


def cmd_build(args) -> None:
    key = load_private(args.private)
    version = release_version()
    data = build_zip()
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    zip_path = os.path.join(out, f"fh_report-{version}.zip")
    with open(zip_path, "wb") as f:
        f.write(data)
    with open(zip_path + ".sig", "w", encoding="ascii") as f:
        f.write(base64.b64encode(key.sign(data)).decode() + "\n")
    print(f"Built {zip_path} and {zip_path}.sig for version {version}.\nAttach both to the release v.{version}.")
    print(f"Public key of this signature: {public_b64(key)}")


def cmd_verify(args) -> None:
    with open(args.zip, "rb") as f:
        data = f.read()
    with open(args.zip + ".sig", encoding="ascii") as f:
        sig = base64.b64decode(f.read().strip())
    pub = args.public or _stored_public_key()
    try:
        Ed25519PublicKey.from_public_bytes(base64.b64decode(pub)).verify(sig, data)
    except Exception:
        sys.exit("INVALID signature")
    print("Signature OK")


def _stored_public_key() -> str:
    with open(os.path.join(ROOT, "plugins", "fh_report", "commands.py"), encoding="utf-8") as f:
        m = re.search(r'^UPGRADE_PUBLIC_KEY\s*=\s*"([^"]*)"', f.read(), re.MULTILINE)
    if not m or not m.group(1):
        sys.exit("UPGRADE_PUBLIC_KEY is empty: pass --public")
    return m.group(1)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    k = sub.add_parser("keygen", help="create the signing key pair (once)")
    k.add_argument("--private", required=True, help="where to write the private key (outside the repo)")
    k.set_defaults(fn=cmd_keygen)
    b = sub.add_parser("build", help="build and sign the release zip")
    b.add_argument("--private", required=True)
    b.add_argument("--out", default=os.path.join(ROOT, "dist"))
    b.set_defaults(fn=cmd_build)
    v = sub.add_parser("verify", help="check a zip against its .sig")
    v.add_argument("zip")
    v.add_argument("--public", help="base64 public key (default: the one in commands.py)")
    v.set_defaults(fn=cmd_verify)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
