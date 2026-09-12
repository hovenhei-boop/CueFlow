from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cueflow.account import AccountService, User
from cueflow.account_migrations import migrate_account_database
from cueflow.account_store import AccountStore
from cueflow.auth import AuthSecrets, AuthService
from cueflow.auth_crypto import (
    FAST_TEST_ARGON2_CONFIG,
    HmacKeyring,
    PasswordHashService,
    SecretKey,
)
from cueflow.phone_reputation import (
    AeadKeyring,
    PhoneReputationCrypto,
    PhoneReputationService,
)
from cueflow.sms import SmsPurpose


class Clock:
    def __init__(self, value: int = 10_000) -> None:
        self.value = value

    def __call__(self) -> int:
        return self.value

    def tick(self, milliseconds: int = 1) -> None:
        self.value += milliseconds


@dataclass
class CapturingSmsProvider:
    sent: list[tuple[str, str, SmsPurpose]]
    fail: bool = False

    def __init__(self) -> None:
        self.sent = []
        self.fail = False

    def send_code(self, *, phone: str, code: str, purpose: SmsPurpose) -> None:
        if self.fail:
            raise RuntimeError("simulated SMS provider failure")
        self.sent.append((phone, code, purpose))

    def last_code(self) -> str:
        return self.sent[-1][1]


@dataclass(frozen=True)
class AuthStack:
    database: Path
    backups: Path
    store: AccountStore
    account: AccountService
    auth: AuthService
    reputations: PhoneReputationService
    hasher: PasswordHashService
    sms: CapturingSmsProvider
    clock: Clock


def make_auth_stack(tmp_path: Path, *, clock: Clock | None = None) -> AuthStack:
    database = (tmp_path / "account.sqlite3").resolve()
    backups = (tmp_path / "backups").resolve()
    migrate_account_database(database, backups, now_ms=1)
    store = AccountStore(database)
    chosen_clock = clock or Clock()
    hasher = PasswordHashService(config=FAST_TEST_ARGON2_CONFIG)
    auth_keys = HmacKeyring(SecretKey("auth-test", b"a" * 32))
    grant_keys = HmacKeyring(SecretKey("grant-test", b"g" * 32))
    token_keys = HmacKeyring(SecretKey("token-test", b"t" * 32))
    reputation_keys = HmacKeyring(SecretKey("reputation-test", b"r" * 32))
    reputation_crypto = PhoneReputationCrypto(
        reputation_keys,
        AeadKeyring(SecretKey("encryption-test", b"e" * 32)),
    )
    reputations = PhoneReputationService(store, reputation_crypto)
    sms = CapturingSmsProvider()
    auth = AuthService(
        store,
        password_hasher=hasher,
        secrets=AuthSecrets(auth_keys, grant_keys, token_keys),
        sms_provider=sms,
        phone_reputations=reputations,
        clock=chosen_clock,
    )
    return AuthStack(
        database,
        backups,
        store,
        AccountService(store, clock=chosen_clock),
        auth,
        reputations,
        hasher,
        sms,
        chosen_clock,
    )


def make_request_auth_service_factory(stack: AuthStack) -> Callable[[], AuthService]:
    """Return request-scoped AuthService instances with independent SQLite connections."""

    def factory() -> AuthService:
        store = AccountStore(stack.database)
        reputations = PhoneReputationService(store, stack.reputations.crypto)
        return AuthService(
            store,
            password_hasher=stack.hasher,
            secrets=stack.auth.secrets,
            sms_provider=stack.sms,
            phone_reputations=reputations,
            clock=stack.clock,
            policy=stack.auth.policy,
        )

    return factory


def make_test_account(
    stack: AuthStack,
    phone: str,
    *,
    password: str = "correct horse battery staple",
) -> User:
    """Build a legal v0.6.1 account without exercising the product registration flow."""
    now = stack.clock()
    user_id = "usr_test_" + uuid.uuid4().hex
    identity_id = "idn_test_" + uuid.uuid4().hex
    password_hash = stack.hasher.hash_password(password)
    with stack.store.transaction() as tx:
        tx.execute("INSERT INTO users VALUES (?, 'active', ?, ?)", (user_id, now, now))
        reputation = stack.reputations.ensure_phone_tx(tx, phone, now=now)
        tx.execute(
            """INSERT INTO auth_identities VALUES (
                ?, ?, 'phone', ?, 'active', 'test_fixture', ?, ?, NULL, NULL, ?
            )""",
            (
                identity_id,
                user_id,
                phone,
                now,
                now,
                reputation.phone_reputation_id,
            ),
        )
        tx.execute(
            "INSERT INTO password_credentials VALUES (?, ?, ?, ?)",
            (user_id, password_hash, now, now),
        )
        tx.execute(
            "INSERT INTO account_audit_events VALUES (?, ?, 'test_account_created', ?, '{}', ?)",
            ("evt_test_" + uuid.uuid4().hex, user_id, identity_id, now),
        )
    return stack.account.get_user(user_id)
