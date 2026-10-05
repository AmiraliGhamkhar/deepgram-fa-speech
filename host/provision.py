"""Provision client identities for the host service.

A hospital running 50 clinicians behind one NAT cannot use a single shared
secret: the host could not tell the doctors apart, could not rate limit
them independently, and revoking one stolen device would revoke all 50.
This tool mints one `client_id` + secret per device and writes **only the
SHA-256 hash** of each secret into the host's registry file.

    # One device (interactive-friendly output: the secret is printed once)
    python -m host.provision --client-id doctor-01

    # 50 devices at once; the secret is written to a per-device file
    python -m host.provision --count 50 --prefix doctor --out clients.txt \
        --secrets-dir ./secrets

Output split, by design:

* `clients.txt` (host side) -- `client_id:sha256hex` lines. Safe to keep in
  configuration management; contains no usable credential.
* `*.secret` (clinician side) -- the plaintext secret, one file per device.
  Transferred to that machine and deleted here.

The plaintext secret is never written to the registry, never logged by the
host, and never recoverable from the host later -- that is the point.
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path
from typing import List, Optional, Sequence

#: Number of random bytes in a client secret. 32 bytes = 256 bits of
#: entropy, which makes an offline attack against the stored SHA-256 hash
#: computationally infeasible.
SECRET_BYTES = 32

#: Refuse absurd counts: a typo like `--count 5000` should fail loudly
#: rather than silently produce a huge registry.
MAX_COUNT = 5_000


def generate_client_id(prefix: str, index: Optional[int] = None) -> str:
    """Build a syntactically valid client id.

    The result must satisfy `core.is_valid_client_id`: only alphanumerics,
    `-`, `_` and `.`, at most 64 characters.
    """
    safe_prefix = "".join(ch for ch in prefix if ch.isalnum() or ch in "-_.")
    if not safe_prefix:
        safe_prefix = "client"
    if index is None:
        suffix = secrets.token_hex(4)
    else:
        suffix = f"{index:03d}"
    return f"{safe_prefix}-{suffix}"[:64]


def generate_secret() -> str:
    """A URL-safe 256-bit client secret."""
    return secrets.token_urlsafe(SECRET_BYTES)


def registry_line(client_id: str, secret: str) -> str:
    """The `client_id:sha256hex` line stored on the host."""
    import hashlib

    digest = hashlib.sha256(secret.encode("utf-8")).hexdigest()
    return f"{client_id}:{digest}"


def _write_secret_file(directory: Path, client_id: str, secret: str) -> Path:
    """Write one plaintext secret file, owner-readable only."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{client_id}.secret"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(f"{secret}\n")
    return path


def _generate(
    args: argparse.Namespace, out: List[str]
) -> int:
    count = max(1, args.count)
    if count > MAX_COUNT:
        print(f"error: --count {count} exceeds the {MAX_COUNT} limit", file=sys.stderr)
        return 2

    ids: List[str] = []
    if args.client_id:
        ids = [args.client_id]
        count = 1
    else:
        seen = set()
        while len(ids) < count:
            candidate = generate_client_id(args.prefix, len(ids) if args.start_index else None)
            if candidate in seen:
                continue
            seen.add(candidate)
            ids.append(candidate)

    registry_path = Path(args.out)
    # Append, so re-provisioning one extra device does not revoke the 50
    # already deployed.
    with registry_path.open("a", encoding="utf-8") as handle:
        if registry_path.stat().st_size:
            handle.write("\n")
        for client_id in ids:
            secret = generate_secret()
            handle.write(registry_line(client_id, secret) + "\n")
            if args.secrets_dir:
                path = _write_secret_file(Path(args.secrets_dir), client_id, secret)
                out.append(f"{client_id} -> {path}")
            else:
                # Printed once, on the operator's terminal, for manual
                # transfer. Never echoed into a log or a shell history file.
                out.append(f"{client_id} -> {secret}")

    print(f"registry updated: {registry_path} ({len(ids)} client(s))")
    for line in out:
        print(f"  {line}")
    if not args.secrets_dir and out:
        print(
            "\nTransfer each secret to its device and delete this output. "
            "It is not recoverable from the host afterwards.",
            file=sys.stderr,
        )
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--client-id", help="provision exactly this client id")
    parser.add_argument("--count", type=int, default=1, help="how many clients to mint")
    parser.add_argument("--prefix", default="doctor", help="client id prefix for --count")
    parser.add_argument(
        "--start-index", action="store_true", help="number ids (doctor-000, doctor-001, ...)"
    )
    parser.add_argument("--out", default="clients.txt", help="registry file to append to")
    parser.add_argument(
        "--secrets-dir",
        help="write each plaintext secret to this directory instead of stdout",
    )
    args = parser.parse_args(argv)

    out: List[str] = []
    return _generate(args, out)


if __name__ == "__main__":  # pragma: no cover - operational entry point
    raise SystemExit(main())
