"""授權 Token 格式（Ed25519 簽章）—— 授權伺服器端（可簽章）。

格式：  ST1.<base64url(payload JSON)>.<base64url(Ed25519 簽章)>
簽章範圍是 "ST1." + payload 部分的 ASCII bytes。

兩種 token：
  type=license     你簽發給客戶的授權（= License Key）。內含方案、到期日、上限、功能。
  type=activation  授權伺服器在「線上啟用 / 續驗」時回傳，綁定客戶的 instance，有租期（lease）。

⚠ 私鑰只能存在授權伺服器（或你自己的電腦），絕對不能放進 rca-engine 或交給客戶。
"""
from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

PREFIX = "ST1"


def b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


# ---------------------------------------------------------------- keys
def generate_keypair() -> tuple[Ed25519PrivateKey, str]:
    """回傳 (私鑰物件, 公鑰 base64url 字串)"""
    priv = Ed25519PrivateKey.generate()
    return priv, public_key_str(priv)


def public_key_str(priv: Ed25519PrivateKey) -> str:
    raw = priv.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return b64e(raw)


def save_private_key(priv: Ed25519PrivateKey, path: Path) -> None:
    pem = priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pem)
    try:
        path.chmod(0o600)
    except OSError:
        pass


def load_private_key(path: Path) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("不是 Ed25519 私鑰")
    return key


# ---------------------------------------------------------------- sign / verify
def sign(payload: dict[str, Any], priv: Ed25519PrivateKey) -> str:
    body = b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True, ensure_ascii=False).encode())
    signing_input = f"{PREFIX}.{body}".encode()
    return f"{PREFIX}.{body}.{b64e(priv.sign(signing_input))}"


def verify(token: str, public_keys: list[str]) -> dict[str, Any]:
    """驗證簽章並回傳 payload；失敗丟 ValueError。public_keys 支援多把（金鑰輪替）。"""
    try:
        prefix, body, sig = token.strip().split(".")
    except ValueError:
        raise ValueError("token 格式錯誤")
    if prefix != PREFIX:
        raise ValueError("不支援的 token 版本")
    signing_input = f"{prefix}.{body}".encode()
    for pk in public_keys:
        try:
            Ed25519PublicKey.from_public_bytes(b64d(pk)).verify(b64d(sig), signing_input)
            return json.loads(b64d(body))
        except (InvalidSignature, ValueError):
            continue
    raise ValueError("簽章驗證失敗")


def peek(token: str) -> dict[str, Any]:
    """不驗證簽章，只解出 payload（除錯 / 顯示用）"""
    return json.loads(b64d(token.strip().split(".")[1]))
