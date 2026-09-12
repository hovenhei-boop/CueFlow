from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Barrier

import pytest

from cueflow.account import (
    ACCOUNT_ERASURE_ORDER,
    MAX_ACTIVE_SESSION_FAMILIES,
    SESSION_FAMILY_ABSOLUTE_TTL_MS,
    AccountService,
    AuthIdentity,
    IdentityProvider,
    User,
    UserStatus,
    VerifiedIdentityClaim,
)
from cueflow.account_migrations import (
    ACCOUNT_OWNED_TABLES,
    SESSION_REVOCATION_REASONS,
)
from cueflow.account_store import AccountStore
from cueflow.errors import (
    AccountNotFoundError,
    AccountStateError,
    ContractError,
    IdentityConflictError,
    SessionStateError,
)
from tests.account_helpers import AuthStack, Clock, make_auth_stack, make_test_account

_STACKS: dict[int, AuthStack] = {}


@dataclass(frozen=True)
class _TestAccount:
    user: User
    phone: AuthIdentity


def _claim(provider: IdentityProvider, subject: str, verified_at: int = 1) -> VerifiedIdentityClaim:
    return VerifiedIdentityClaim(provider, subject, "test_verified_claim", verified_at)


def _digest(index: int) -> str:
    return f"hmac-sha256:v1:{index:064x}"


def _open_service(tmp_path: Path) -> tuple[Path, Path, AccountStore, AccountService, Clock]:
    stack = make_auth_stack(tmp_path)
    _STACKS[id(stack.store)] = stack
    return stack.database, stack.backups, stack.store, stack.account, stack.clock


def _make_account(service: AccountService, claim: VerifiedIdentityClaim) -> _TestAccount:
    if claim.provider is not IdentityProvider.PHONE:
        raise ContractError("test accounts require a phone claim")
    phone = claim.provider_subject
    stack = _STACKS[id(service.store)]
    user = make_test_account(stack, phone)
    identity = service.find_active_identity(IdentityProvider.PHONE, phone)
    assert identity is not None
    return _TestAccount(user, identity)


def test_complete_account_has_exactly_one_e164_phone_and_can_attach_identity(
    tmp_path: Path,
) -> None:
    _, _, store, service, _ = _open_service(tmp_path)
    try:
        with pytest.raises(ContractError, match="E.164"):
            _make_account(service, _claim(IdentityProvider.PHONE, "13812345678"))
        account = _make_account(service, _claim(IdentityProvider.PHONE, "+8613812345678"))
        service.attach_identity(
            account.user.user_id, _claim(IdentityProvider.EMAIL, "person@example.com")
        )
        active = service.list_identities(account.user.user_id)
        all_identities = service.list_identities(account.user.user_id, include_detached=True)
        assert "identity_linked" in {
            event.event_type for event in service.list_audit_events(account.user.user_id)
        }
        assert "identity_attached" not in {
            event.event_type for event in service.list_audit_events(account.user.user_id)
        }
        assert [identity.provider for identity in active].count(IdentityProvider.PHONE) == 1
        assert len(all_identities) == 2
    finally:
        store.close()


def test_two_independent_connections_cannot_bind_one_identity_twice(tmp_path: Path) -> None:
    database, _, store, service, _ = _open_service(tmp_path)
    first = _make_account(service, _claim(IdentityProvider.PHONE, "+8613810000001"))
    second = _make_account(service, _claim(IdentityProvider.PHONE, "+8613810000002"))
    store.close()
    barrier = Barrier(2)

    def attach(user_id: str) -> str:
        worker_store = AccountStore(database)
        try:
            worker_service = AccountService(worker_store, clock=lambda: 20_000)
            barrier.wait()
            worker_service.attach_identity(
                user_id, _claim(IdentityProvider.WECHAT, "wx_same_subject")
            )
            return "attached"
        except IdentityConflictError:
            return "conflict"
        finally:
            worker_store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(attach, (first.user.user_id, second.user.user_id)))

    assert sorted(outcomes) == ["attached", "conflict"]
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            """SELECT COUNT(*) FROM auth_identities
            WHERE provider='wechat' AND provider_subject='wx_same_subject' AND status='active'"""
        ).fetchone() == (1,)


def test_sixth_session_evicts_oldest_family_and_the_transition_is_atomic(
    tmp_path: Path,
) -> None:
    _, _, store, service, clock = _open_service(tmp_path)
    try:
        user = _make_account(service, _claim(IdentityProvider.PHONE, "+8613810000010")).user
        sessions = []
        for index in range(MAX_ACTIVE_SESSION_FAMILIES):
            sessions.append(
                service.create_session(
                    user.user_id,
                    refresh_token_hash=_digest(index),
                    expires_at=100_000,
                )
            )
            clock.tick()

        store.connection.execute(
            """CREATE TEMP TRIGGER fail_eviction_audit
            BEFORE INSERT ON account_audit_events
            WHEN NEW.event_type = 'session_family_evicted'
            BEGIN SELECT RAISE(ABORT, 'test audit failure'); END"""
        )
        with pytest.raises(SessionStateError):
            service.create_session(user.user_id, refresh_token_hash=_digest(5), expires_at=100_000)
        assert service.get_session(sessions[0].session_id).revoked_at is None
        assert store.connection.execute("SELECT COUNT(*) FROM session_families").fetchone()[0] == 5
        store.connection.execute("DROP TRIGGER fail_eviction_audit")

        newest = service.create_session(
            user.user_id, refresh_token_hash=_digest(5), expires_at=100_000
        )

        assert newest.revoked_at is None
        assert service.get_session(sessions[0].session_id).revoked_at == clock.value
        family = store.session_family(sessions[0].session_family_id)
        assert family["revocation_reason"] == "session_limit_eviction"
        active_families = store.connection.execute(
            "SELECT COUNT(*) FROM session_families WHERE revoked_at IS NULL"
        ).fetchone()
        assert active_families[0] == MAX_ACTIVE_SESSION_FAMILIES
        evictions = [
            event
            for event in service.list_audit_events(user.user_id)
            if event.event_type == "session_family_evicted"
        ]
        assert len(evictions) == 1
        assert evictions[0].subject_id == sessions[0].session_family_id
    finally:
        store.close()


def test_two_connections_cannot_exceed_the_active_session_family_limit(
    tmp_path: Path,
) -> None:
    database, _, store, service, clock = _open_service(tmp_path)
    user = _make_account(service, _claim(IdentityProvider.PHONE, "+8613810000015")).user
    original_sessions = []
    for index in range(4):
        original_sessions.append(
            service.create_session(
                user.user_id,
                refresh_token_hash=_digest(100 + index),
                expires_at=100_000,
            )
        )
        clock.tick()
    store.close()
    barrier = Barrier(2)

    def create(index: int) -> tuple[str, str, str]:
        worker_store = AccountStore(database)
        try:
            worker_service = AccountService(worker_store, clock=lambda: 20_000)
            barrier.wait()
            session = worker_service.create_session(
                user.user_id,
                refresh_token_hash=_digest(index),
                expires_at=100_000,
            )
            return session.session_id, session.session_family_id, session.refresh_token_hash
        finally:
            worker_store.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        created = list(executor.map(create, (200, 201)))

    verification_store = AccountStore(database)
    try:
        verification_service = AccountService(verification_store, clock=lambda: 20_000)
        active_count = verification_store.connection.execute(
            """SELECT COUNT(*) FROM session_families f
            WHERE f.user_id=? AND f.revoked_at IS NULL
              AND EXISTS (
                SELECT 1 FROM sessions s
                WHERE s.session_family_id=f.session_family_id
                  AND s.revoked_at IS NULL
                  AND s.replaced_by_session_id IS NULL
                  AND s.expires_at>?
              )""",
            (user.user_id, 20_000),
        ).fetchone()[0]
        assert active_count == MAX_ACTIVE_SESSION_FAMILIES
        assert verification_service.get_session(original_sessions[0].session_id).revoked_at
        assert all(
            verification_service.find_active_session_by_refresh_hash(digest) is not None
            for _, _, digest in created
        )
        events = verification_service.list_audit_events(user.user_id)
        assert sum(event.event_type == "session_family_evicted" for event in events) == 1
    finally:
        verification_store.close()


def test_suspension_blocks_sessions_and_reactivation_never_revives_old_sessions(
    tmp_path: Path,
) -> None:
    _, _, store, service, clock = _open_service(tmp_path)
    try:
        user = _make_account(service, _claim(IdentityProvider.PHONE, "+8613810000020")).user
        old = service.create_session(
            user.user_id, refresh_token_hash=_digest(20), expires_at=100_000
        )
        clock.tick()
        service.set_user_status(user.user_id, UserStatus.SUSPENDED)

        assert service.get_session(old.session_id).revoked_at == clock.value
        with pytest.raises(AccountStateError, match="suspended"):
            service.create_session(user.user_id, refresh_token_hash=_digest(21), expires_at=100_000)
        with pytest.raises(SessionStateError, match="cannot be rotated"):
            service.rotate_session(
                old.session_id,
                new_refresh_token_hash=_digest(22),
                new_expires_at=100_000,
            )

        clock.tick()
        service.set_user_status(user.user_id, UserStatus.ACTIVE)
        assert service.get_session(old.session_id).revoked_at is not None
        with pytest.raises(SessionStateError):
            service.rotate_session(
                old.session_id,
                new_refresh_token_hash=_digest(23),
                new_expires_at=100_000,
            )
        assert (
            service.create_session(
                user.user_id, refresh_token_hash=_digest(24), expires_at=100_000
            ).revoked_at
            is None
        )
    finally:
        store.close()


def test_reused_rotated_token_can_revoke_the_whole_family(tmp_path: Path) -> None:
    _, _, store, service, clock = _open_service(tmp_path)
    try:
        user = _make_account(service, _claim(IdentityProvider.PHONE, "+8613810000030")).user
        old_hash = _digest(30)
        old = service.create_session(user.user_id, refresh_token_hash=old_hash, expires_at=100_000)
        clock.tick()
        current = service.rotate_session(
            old.session_id,
            new_refresh_token_hash=_digest(31),
            new_expires_at=100_000,
        )

        reused = service.find_session_by_refresh_hash(old_hash)
        assert reused is not None
        assert reused.replaced_by_session_id == current.session_id
        clock.tick()
        store.connection.execute(
            """CREATE TEMP TRIGGER fail_reuse_revoke_audit
            BEFORE INSERT ON account_audit_events
            WHEN NEW.event_type = 'session_family_revoked'
            BEGIN SELECT RAISE(ABORT, 'test reuse audit failure'); END"""
        )
        with pytest.raises(sqlite3.IntegrityError, match="test reuse audit failure"):
            service.revoke_session_family(reused.session_family_id, reason="refresh_token_reuse")
        assert store.session_family(current.session_family_id)["revoked_at"] is None
        assert service.get_session(current.session_id).revoked_at is None
        assert not any(
            event.event_type == "token_reuse_detected"
            for event in service.list_audit_events(user.user_id)
        )
        store.connection.execute("DROP TRIGGER fail_reuse_revoke_audit")

        changed = service.revoke_session_family(
            reused.session_family_id, reason="refresh_token_reuse"
        )

        assert changed == 1
        assert service.get_session(current.session_id).revoked_at == clock.value
        family = store.session_family(current.session_family_id)
        assert family["revocation_reason"] == "refresh_token_reuse"
        first_events = service.list_audit_events(user.user_id)
        assert sum(event.event_type == "token_reuse_detected" for event in first_events) == 1
        assert sum(event.event_type == "session_family_revoked" for event in first_events) == 1

        assert (
            service.revoke_session_family(reused.session_family_id, reason="refresh_token_reuse")
            == 0
        )
        repeated_events = service.list_audit_events(user.user_id)
        assert sum(event.event_type == "token_reuse_detected" for event in repeated_events) == 2
        assert sum(event.event_type == "session_family_revoked" for event in repeated_events) == 1
    finally:
        store.close()


def test_session_expiry_boundary_is_now_greater_than_or_equal(tmp_path: Path) -> None:
    _, _, store, service, clock = _open_service(tmp_path)
    try:
        user = _make_account(service, _claim(IdentityProvider.PHONE, "+8613810000035")).user
        with pytest.raises(ContractError, match="future"):
            service.create_session(
                user.user_id, refresh_token_hash=_digest(35), expires_at=clock.value
            )
        active = service.create_session(
            user.user_id,
            refresh_token_hash=_digest(36),
            expires_at=clock.value + 1,
        )
        clock.tick()
        with pytest.raises(SessionStateError, match="cannot be rotated"):
            service.rotate_session(
                active.session_id,
                new_refresh_token_hash=_digest(37),
                new_expires_at=100_000,
            )
    finally:
        store.close()


def test_session_family_absolute_deadline_is_part_of_the_shared_validity_predicate(
    tmp_path: Path,
) -> None:
    _, _, store, service, clock = _open_service(tmp_path)
    try:
        clock.value = SESSION_FAMILY_ABSOLUTE_TTL_MS + 10_000
        user = _make_account(service, _claim(IdentityProvider.PHONE, "+8613810000038")).user
        session = service.create_session(
            user.user_id,
            refresh_token_hash=_digest(380),
            expires_at=clock.value + SESSION_FAMILY_ABSOLUTE_TTL_MS + 1,
        )
        with store.transaction() as tx:
            tx.execute(
                "UPDATE session_families SET created_at=? WHERE session_family_id=?",
                (clock.value - SESSION_FAMILY_ABSOLUTE_TTL_MS + 1, session.session_family_id),
            )
        assert service.session_is_active(session.session_id)
        with store.transaction() as tx:
            tx.execute(
                "UPDATE session_families SET created_at=? WHERE session_family_id=?",
                (clock.value - SESSION_FAMILY_ABSOLUTE_TTL_MS, session.session_family_id),
            )
        assert not service.session_is_active(session.session_id)
        assert service.find_active_session_by_refresh_hash(session.refresh_token_hash) is None
        with pytest.raises(SessionStateError, match="cannot be rotated"):
            service.rotate_session(
                session.session_id,
                new_refresh_token_hash=_digest(381),
                new_expires_at=clock.value + 1,
            )
    finally:
        store.close()


def test_session_validity_has_one_authoritative_predicate(
    tmp_path: Path,
) -> None:
    _, _, store, service, clock = _open_service(tmp_path)
    try:
        user = _make_account(service, _claim(IdentityProvider.PHONE, "+8613810000036")).user
        valid = service.create_session(
            user.user_id, refresh_token_hash=_digest(360), expires_at=100_000
        )
        assert service.find_session_by_refresh_hash(valid.refresh_token_hash) == valid
        assert service.find_active_session_by_refresh_hash(valid.refresh_token_hash) == valid
        assert service.session_is_active(valid.session_id)

        revoked = service.create_session(
            user.user_id, refresh_token_hash=_digest(361), expires_at=100_000
        )
        service.revoke_session(revoked.session_id)
        assert service.find_session_by_refresh_hash(revoked.refresh_token_hash) is not None
        assert service.find_active_session_by_refresh_hash(revoked.refresh_token_hash) is None
        assert not service.session_is_active(revoked.session_id)

        replaced = service.create_session(
            user.user_id, refresh_token_hash=_digest(362), expires_at=100_000
        )
        replacement = service.rotate_session(
            replaced.session_id,
            new_refresh_token_hash=_digest(363),
            new_expires_at=100_000,
        )
        raw_replaced = service.find_session_by_refresh_hash(replaced.refresh_token_hash)
        assert raw_replaced is not None
        assert raw_replaced.replaced_by_session_id == replacement.session_id
        assert service.find_active_session_by_refresh_hash(replaced.refresh_token_hash) is None
        assert not service.session_is_active(replaced.session_id)

        expired = service.create_session(
            user.user_id,
            refresh_token_hash=_digest(364),
            expires_at=clock.value + 1,
        )
        clock.tick()
        assert service.find_session_by_refresh_hash(expired.refresh_token_hash) is not None
        assert service.find_active_session_by_refresh_hash(expired.refresh_token_hash) is None
        assert not service.session_is_active(expired.session_id)

        suspended_user = _make_account(
            service, _claim(IdentityProvider.PHONE, "+8613810000037")
        ).user
        suspended_session = service.create_session(
            suspended_user.user_id,
            refresh_token_hash=_digest(365),
            expires_at=100_000,
        )
        with store.transaction() as tx:
            tx.execute(
                "UPDATE users SET status='suspended', updated_at=? WHERE user_id=?",
                (clock.value, suspended_user.user_id),
            )
        assert service.find_session_by_refresh_hash(suspended_session.refresh_token_hash)
        assert (
            service.find_active_session_by_refresh_hash(suspended_session.refresh_token_hash)
            is None
        )
        assert not service.session_is_active(suspended_session.session_id)

        for inactive in (revoked, replaced, expired, suspended_session):
            with pytest.raises(SessionStateError, match="cannot be rotated"):
                service.rotate_session(
                    inactive.session_id,
                    new_refresh_token_hash=_digest(400 + len(inactive.session_id)),
                    new_expires_at=100_000,
                )
    finally:
        store.close()


def test_active_account_erasure_removes_every_owned_row_and_makes_identities_reusable(
    tmp_path: Path,
) -> None:
    _, backups, store, service, clock = _open_service(tmp_path)
    try:
        phone = "+8613810000040"
        account = _make_account(service, _claim(IdentityProvider.PHONE, phone))
        service.attach_identity(
            account.user.user_id, _claim(IdentityProvider.APPLE, "apple_subject")
        )
        service.create_session(
            account.user.user_id, refresh_token_hash=_digest(40), expires_at=100_000
        )
        backups_before = set(backups.glob("*.sqlite3"))
        service.set_user_status(account.user.user_id, UserStatus.SUSPENDED)
        with pytest.raises(AccountStateError, match="suspended"):
            service.erase_account(account.user.user_id)
        clock.tick()
        service.set_user_status(account.user.user_id, UserStatus.ACTIVE)

        service.erase_account(account.user.user_id)

        with pytest.raises(AccountNotFoundError):
            service.get_user(account.user.user_id)
        for table in ACCOUNT_ERASURE_ORDER:
            assert (
                store.connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE user_id=?", (account.user.user_id,)
                ).fetchone()[0]
                == 0
            )
        assert set(backups.glob("*.sqlite3")) == backups_before
        assert service.find_session_by_refresh_hash(_digest(40)) is None
        assert _STACKS[id(store)].reputations.find_by_phone(phone) is not None
        assert (
            _make_account(service, _claim(IdentityProvider.PHONE, phone)).phone.provider_subject
            == phone
        )
    finally:
        store.close()


def test_account_owned_table_erasure_registry_is_complete() -> None:
    assert frozenset(ACCOUNT_ERASURE_ORDER) == ACCOUNT_OWNED_TABLES


def test_reason_codes_are_finite_and_arbitrary_text_is_rejected(tmp_path: Path) -> None:
    assert SESSION_REVOCATION_REASONS == {
        "user_request",
        "refresh_token_reuse",
        "user_suspended",
        "session_limit_eviction",
        "administrative_revoke",
        "logout_all",
        "password_changed",
        "password_reset",
        "phone_changed",
    }
    _, _, store, service, _ = _open_service(tmp_path)
    try:
        user = _make_account(service, _claim(IdentityProvider.PHONE, "+8613810000045")).user
        session = service.create_session(
            user.user_id, refresh_token_hash=_digest(450), expires_at=100_000
        )
        with pytest.raises(ContractError, match="reason is not allowed"):
            service.revoke_session(session.session_id, reason="+8613812345678")
    finally:
        store.close()


def test_nested_account_transaction_is_a_contract_error(tmp_path: Path) -> None:
    _, _, store, _, _ = _open_service(tmp_path)
    try:
        with store.transaction():
            with pytest.raises(ContractError, match="nested AccountStore"):
                with store.transaction():
                    raise AssertionError("nested transaction must not start")
    finally:
        store.close()


@pytest.mark.parametrize(
    "invalid_digest",
    (
        "sha256:" + "a" * 64,
        "hmac-sha256:k1:not-hex",
        "hmac-sha256:k1:" + "a" * 63,
        "hmac-sha256:k1:" + "a" * 65,
        "hmac-sha256::" + "a" * 64,
    ),
)
def test_account_core_rejects_noncanonical_refresh_token_digests(
    tmp_path: Path, invalid_digest: str
) -> None:
    _, _, store, service, _ = _open_service(tmp_path)
    try:
        user = _make_account(service, _claim(IdentityProvider.PHONE, "+8613810000050")).user
        with pytest.raises(ContractError, match="digest"):
            service.create_session(
                user.user_id, refresh_token_hash=invalid_digest, expires_at=100_000
            )
    finally:
        store.close()
