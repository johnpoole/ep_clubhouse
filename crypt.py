#!/usr/bin/env python3
"""
Encrypt/decrypt deployment files for the ep_clubhouse repo.

Files are encrypted with AES-256-CBC using a symmetric key stored in
.encryption_key (never committed to git).

Usage:
    python crypt.py encrypt          # encrypt all tracked files -> .enc
    python crypt.py decrypt          # decrypt .enc files -> plaintext
    python crypt.py encrypt-push     # encrypt + git add + commit + push
"""

import hashlib
import os
import struct
import sys
from pathlib import Path

REPO_DIR = Path(__file__).parent
KEY_FILE = REPO_DIR / ".encryption_key"

# Files to encrypt (source -> encrypted name in repo)
PROTECTED_FILES = [
    "yarbo_bridge.py",
    "requirements.txt",
    "pi/deploy.sh",
    "pi/update.sh",
    "pi/yarbo-bridge.service",
    "pi/yarbo-bridge-update.service",
    "pi/yarbo-bridge-update.timer",
]


def get_key() -> bytes:
    """Read the encryption key and derive a 32-byte AES key."""
    if not KEY_FILE.exists():
        print(f"ERROR: Key file not found: {KEY_FILE}")
        print("Copy .encryption_key from a trusted machine.")
        sys.exit(1)
    raw = KEY_FILE.read_text().strip()
    # Derive a proper 32-byte key using SHA-256
    return hashlib.sha256(raw.encode()).digest()


def pad(data: bytes) -> bytes:
    """PKCS7 padding to 16-byte boundary."""
    pad_len = 16 - (len(data) % 16)
    return data + bytes([pad_len] * pad_len)


def unpad(data: bytes) -> bytes:
    """Remove PKCS7 padding."""
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 16:
        raise ValueError("Invalid padding")
    return data[:-pad_len]


def encrypt_file(src: Path, dst: Path, key: bytes):
    """Encrypt a file with AES-256-CBC."""
    import secrets
    iv = secrets.token_bytes(16)
    plaintext = src.read_bytes()

    # Use OpenSSL-compatible format via subprocess (works everywhere)
    import subprocess
    openssl = _find_openssl()
    result = subprocess.run(
        [openssl, "enc", "-aes-256-cbc", "-salt",
         "-pass", f"pass:{KEY_FILE.read_text().strip()}",
         "-in", str(src), "-out", str(dst)],
        capture_output=True
    )
    if result.returncode != 0:
        print(f"  ERROR encrypting {src}: {result.stderr.decode()}")
        return False
    return True


def decrypt_file(src: Path, dst: Path, key: bytes):
    """Decrypt a file with AES-256-CBC."""
    import subprocess
    openssl = _find_openssl()
    result = subprocess.run(
        [openssl, "enc", "-aes-256-cbc", "-d", "-salt",
         "-pass", f"pass:{KEY_FILE.read_text().strip()}",
         "-in", str(src), "-out", str(dst)],
        capture_output=True
    )
    if result.returncode != 0:
        print(f"  ERROR decrypting {src}: {result.stderr.decode()}")
        return False
    return True


def _find_openssl() -> str:
    """Find OpenSSL binary."""
    import shutil
    # Git for Windows bundles OpenSSL
    git_openssl = r"C:\Program Files\Git\usr\bin\openssl.exe"
    if os.path.exists(git_openssl):
        return git_openssl
    found = shutil.which("openssl")
    if found:
        return found
    print("ERROR: OpenSSL not found. Install Git for Windows or OpenSSL.")
    sys.exit(1)


def cmd_encrypt():
    """Encrypt all protected files."""
    key = get_key()
    for rel in PROTECTED_FILES:
        src = REPO_DIR / rel
        dst = REPO_DIR / (rel + ".enc")
        if not src.exists():
            print(f"  SKIP (missing): {rel}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if encrypt_file(src, dst, key):
            print(f"  encrypted: {rel} -> {rel}.enc")


def cmd_decrypt():
    """Decrypt all .enc files."""
    key = get_key()
    for rel in PROTECTED_FILES:
        src = REPO_DIR / (rel + ".enc")
        dst = REPO_DIR / rel
        if not src.exists():
            print(f"  SKIP (no .enc): {rel}")
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if decrypt_file(src, dst, key):
            print(f"  decrypted: {rel}.enc -> {rel}")


def cmd_encrypt_push():
    """Encrypt, commit, and push."""
    import subprocess
    cmd_encrypt()
    print("\nCommitting and pushing...")
    subprocess.run(["git", "add", "."], cwd=REPO_DIR, check=True)
    subprocess.run(["git", "commit", "-m", "Update encrypted files"], cwd=REPO_DIR)
    subprocess.run(["git", "push"], cwd=REPO_DIR, check=True)
    print("Done!")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python crypt.py [encrypt|decrypt|encrypt-push]")
        sys.exit(1)

    cmd = sys.argv[1]
    if cmd == "encrypt":
        cmd_encrypt()
    elif cmd == "decrypt":
        cmd_decrypt()
    elif cmd == "encrypt-push":
        cmd_encrypt_push()
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)
