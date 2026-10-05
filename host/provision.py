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
from typing import Any, List, Optional, Sequence

#: Number of random bytes in a client secret. 32 bytes = 256 bits of
#: entropy, which makes an offline attack against the stored SHA-256 hash
#: computationally infeasible.
SECRET_BYTES = 32

#: Refuse absurd counts: a typo like `--count 5000` should fail loudly
#: rather than silently produce a huge registry.
MAX_COUNT = 5_000

#: A generated id is `<safe_prefix>-<suffix>`; the fixed-width suffix and
#: its separator need 5 characters. A longer prefix would be truncated by
#: `generate_client_id`, making every id identical -- so it is refused
#: up front instead of hanging the generation loop forever.
MAX_PREFIX_LENGTH = 59  # 64 - 5

#: Upper bound on candidate ids tried to satisfy `--count` after collisions
#: (a random suffix that was seen before, or a truncated id). Without it, a
#: pathological prefix makes the loop never terminate and the operator's
#: provisioning session hangs indefinitely.
MAX_DEDUP_ATTEMPTS = MAX_COUNT * 2


def _is_valid_client_id(client_id: Any) -> bool:
    """Mirror of `host.core.is_valid_client_id`.

    Duplicated deliberately rather than imported: this tool must run
    standalone (it has no web-framework dependency and `host` ships no
    `__init__.py`), and the client package makes the same trade-off. The
    registry is rejected wholesale by `ClientRegistry.from_file` if any
    line is invalid, so an id this tool cannot mint must never be written.
    """
    if not isinstance(client_id, str) or not 1 <= len(client_id) <= 64:
        return False
    return all(ch.isalnum() or ch in "-." or ch == "_" for ch in client_id)


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


def _existing_registry_ids(registry_path: Path) -> set:
    """Client ids already present in the registry file (best effort).

    Used only to warn about re-provisioning: appending a duplicate line
    would silently replace that device's stored hash, and the old secret
    would stop working without any notice.
    """
    try:
        text = registry_path.read_text(encoding="utf-8")
    except OSError:
        return set()
    ids = set()
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            ids.add(line.partition(":")[0].strip())
    return ids


def _generate(
    args: argparse.Namespace, out: List[str]
) -> int:
    count = max(1, args.count)
    if count > MAX_COUNT:
        print(f"error: --count {count} exceeds the {MAX_COUNT} limit", file=sys.stderr)
        return 2

    if args.client_id:
        # `ClientRegistry.from_file` rejects the *whole registry* on any
        # invalid id, so one typo here would take down every deployed
        # device at the host's next restart. Refuse it now instead.
        if not _is_valid_client_id(args.client_id):
            print(
                f"error: --client-id {args.client_id!r} is not valid: use 1-64 "
                "characters of letters, digits, '-', '_' or '.'",
                file=sys.stderr,
            )
            return 2
        ids = [args.client_id]
    else:
        safe_prefix = "".join(ch for ch in args.prefix if ch.isalnum() or ch in "-." or ch == "_")
        if len(safe_prefix) > MAX_PREFIX_LENGTH:
            print(
                f"error: --prefix {args.prefix!r} is too long: generated ids are "
                f"truncated at 64 characters, so a prefix longer than "
                f"{MAX_PREFIX_LENGTH} characters would produce identical ids. "
                "Use a shorter --prefix.",
                file=sys.stderr,
            )
            return 2
        ids = []
        seen = set()
        attempts = 0
        while len(ids) < count:
            attempts += 1
            if attempts > MAX_DEDUP_ATTEMPTS:
                print(
                    f"error: could not generate {count} unique id(s) after "
                    f"{MAX_DEDUP_ATTEMPTS} attempts; the prefix is likely too "
                    "long or the ids are colliding",
                    file=sys.stderr,
                )
                return 2
            candidate = generate_client_id(args.prefix, len(ids) if args.start_index else None)
            if candidate in seen:
                continue
            seen.add(candidate)
            ids.append(candidate)

    registry_path = Path(args.out)
    existing_ids = _existing_registry_ids(registry_path)
    duplicates = [client_id for client_id in ids if client_id in existing_ids]
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

    if duplicates:
        # Loud, on stderr, and non-fatal: the operator chose to re-provision
        # these ids, but the devices that hold the *old* secrets are now
        # locked out and must be told why.
        print(
            "warning: re-provisioned existing client id(s) " + ", ".join(duplicates)
            + " -- the previously issued secret(s) for them no longer work",
            file=sys.stderr,
        )

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
