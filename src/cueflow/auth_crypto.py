from __future__ import annotations

import hashlib
import hmac
import secrets
import unicodedata
from dataclasses import dataclass, field
from typing import Protocol

from argon2 import PasswordHasher as Argon2PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from argon2.low_level import Type

from cueflow.errors import ContractError


@dataclass(frozen=True)
class Argon2Config:
    memory_cost_kib: int
    time_cost: int
    parallelism: int
    hash_len: int = 32
    salt_len: int = 16


PRODUCTION_ARGON2_CONFIG = Argon2Config(
    memory_cost_kib=64 * 1024,
    time_cost=3,
    parallelism=4,
)

# Tests must inject this explicitly. No environment variable can select it.
FAST_TEST_ARGON2_CONFIG = Argon2Config(
    memory_cost_kib=8,
    time_cost=1,
    parallelism=1,
    hash_len=16,
    salt_len=8,
)


class PasswordBlocklist(Protocol):
    def is_blocked(self, password: str) -> bool: ...


@dataclass(frozen=True)
class LocalPasswordBlocklist:
    values: frozenset[str] = frozenset(
        {
            "123456789012345",
            "passwordpassword",
            "qwertyuiop12345",
        }
    )

    def is_blocked(self, password: str) -> bool:
        return password.casefold() in self.values


@dataclass(frozen=True)
class PasswordVerification:
    valid: bool
    replacement_hash: str | None = field(default=None, repr=False)


class PasswordHashService:
    def __init__(
        self,
        *,
        config: Argon2Config = PRODUCTION_ARGON2_CONFIG,
        blocklist: PasswordBlocklist | None = None,
    ) -> None:
        self.config = config
        self.blocklist = blocklist or LocalPasswordBlocklist()
        self._hasher = Argon2PasswordHasher(
            time_cost=config.time_cost,
            memory_cost=config.memory_cost_kib,
            parallelism=config.parallelism,
            hash_len=config.hash_len,
            salt_len=config.salt_len,
            type=Type.ID,
        )

    def normalize_and_validate(self, password: str) -> str:
        normalized = unicodedata.normalize("NFC", password)
        if not 15 <= len(normalized) <= 128:
            raise ContractError("password must contain between 15 and 128 Unicode codepoints")
        if self.blocklist.is_blocked(normalized):
            raise ContractError("password is blocked because it is commonly compromised")
        return normalized

    def hash_password(self, password: str) -> str:
        return self._hasher.hash(self.normalize_and_validate(password))

    def verify_password(self, encoded_hash: str, password: str) -> PasswordVerification:
        normalized = unicodedata.normalize("NFC", password)
        if len(normalized) > 128:
            return PasswordVerification(False)
        try:
            valid = self._hasher.verify(encoded_hash, normalized)
        except (VerifyMismatchError, InvalidHashError, VerificationError):
            return PasswordVerification(False)
        if not valid:
            return PasswordVerification(False)
        replacement = (
            self._hasher.hash(normalized) if self._hasher.check_needs_rehash(encoded_hash) else None
        )
        return PasswordVerification(True, replacement)


@dataclass(frozen=True)
class SecretKey:
    key_id: str
    value: bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not self.key_id or len(self.key_id) > 32:
            raise ContractError("secret key id is invalid")
        if len(self.value) < 32:
            raise ContractError("secret keys must contain at least 256 bits")


class HmacKeyring:
    def __init__(self, active: SecretKey, previous: tuple[SecretKey, ...] = ()) -> None:
        values = (active, *previous)
        if len({item.key_id for item in values}) != len(values):
            raise ContractError("HMAC key ids must be unique")
        self.active = active
        self._keys = {item.key_id: item for item in values}

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(self._keys)

    def key(self, key_id: str) -> SecretKey:
        try:
            return self._keys[key_id]
        except KeyError as exc:
            raise ContractError("unknown HMAC key id") from exc

    def digest(self, purpose: str, value: bytes, *, key_id: str | None = None) -> bytes:
        key = self.active if key_id is None else self.key(key_id)
        payload = _canonical_fields(purpose.encode("ascii"), value)
        return hmac.new(key.value, payload, hashlib.sha256).digest()

    def token_digest(self, purpose: str, raw_token: str, *, key_id: str | None = None) -> str:
        key = self.active if key_id is None else self.key(key_id)
        digest = self.digest(purpose, raw_token.encode("ascii"), key_id=key.key_id)
        return f"hmac-sha256:{key.key_id}:{digest.hex()}"

    def verify_token_digest(self, purpose: str, raw_token: str, encoded: str) -> bool:
        parts = encoded.split(":")
        if len(parts) != 3 or parts[0] != "hmac-sha256":
            return False
        try:
            expected = self.token_digest(purpose, raw_token, key_id=parts[1])
        except (ContractError, UnicodeEncodeError):
            return False
        return hmac.compare_digest(expected, encoded)


def new_opaque_token() -> str:
    return secrets.token_urlsafe(32)


def new_sms_code() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def bound_grant_hash(
    keyring: HmacKeyring,
    *,
    raw_grant: str,
    grant_id: str,
    phone_key: bytes,
    purpose: str,
    expires_at: int,
    key_id: str | None = None,
) -> tuple[str, bytes]:
    key = keyring.active if key_id is None else keyring.key(key_id)
    payload = _canonical_fields(
        b"cueflow:registration-grant:v1",
        raw_grant.encode("ascii"),
        grant_id.encode("ascii"),
        phone_key,
        purpose.encode("ascii"),
        str(expires_at).encode("ascii"),
    )
    return key.key_id, hmac.new(key.value, payload, hashlib.sha256).digest()


def request_fingerprint(*values: str) -> bytes:
    return hashlib.sha256(
        _canonical_fields(
            b"cueflow:phone-reputation-operation:v1", *(v.encode("utf-8") for v in values)
        )
    ).digest()


def _canonical_fields(*values: bytes) -> bytes:
    return b"".join(len(value).to_bytes(4, "big") + value for value in values)
