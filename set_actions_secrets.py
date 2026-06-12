"""One-time helper: push the three Actions secrets to the GitHub repo.

Reads DISCORD_WEBHOOK_URL and KALSHI_KEY_ID from .env and the PEM from
kalshi_private_key.pem, encrypts each with the repo's libsodium public key,
and PUTs them via the REST API. Auth comes from Git Credential Manager
(the token stored by the initial `git push`). Never prints secret values.
"""

import base64
import subprocess
import sys
from pathlib import Path

import requests
from nacl import encoding, public

REPO = "Niconomics-9/FedererBot---Tennis-Anomaly-Scanner"
HERE = Path(__file__).parent


def gcm_token() -> str:
    out = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        capture_output=True, text=True, check=True,
    ).stdout
    return dict(line.split("=", 1) for line in out.strip().splitlines())["password"]


def env_value(key: str) -> str:
    for line in (HERE / ".env").read_text().splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1].strip().strip('"').strip("'")
    sys.exit(f"missing {key} in .env")


def encrypt(repo_key_b64: str, value: str) -> str:
    pk = public.PublicKey(repo_key_b64.encode(), encoding.Base64Encoder())
    return base64.b64encode(public.SealedBox(pk).encrypt(value.encode())).decode()


token = gcm_token()
headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}

r = requests.get(f"https://api.github.com/repos/{REPO}/actions/secrets/public-key", headers=headers)
r.raise_for_status()
repo_key = r.json()

secrets = {
    "DISCORD_WEBHOOK_URL": env_value("DISCORD_WEBHOOK_URL"),
    "KALSHI_KEY_ID": env_value("KALSHI_KEY_ID"),
    "KALSHI_PRIVATE_KEY_PEM": (HERE / "kalshi_private_key.pem").read_text(),
}

for name, value in secrets.items():
    resp = requests.put(
        f"https://api.github.com/repos/{REPO}/actions/secrets/{name}",
        headers=headers,
        json={"encrypted_value": encrypt(repo_key["key"], value), "key_id": repo_key["key_id"]},
    )
    print(f"{name}: HTTP {resp.status_code}")
    resp.raise_for_status()

print("all secrets set")
