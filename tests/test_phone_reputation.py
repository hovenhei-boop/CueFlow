from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from cueflow.auth_crypto import HmacKeyring, SecretKey
from cueflow.errors import AccountStateError, PhoneReputationIntegrityError
from cueflow.phone_reputation import PhoneReputationCrypto, PhoneReputationService, SanctionActor
from cueflow.sms import SmsPurpose
from tests.account_helpers import make_auth_stack, make_test_account


def _erase_banned_account(stack: object, phone: str) -> None:
    auth = stack.auth  # type: ignore[attr-defined]
    sms = stack.sms  # type: ignore[attr-defined]
    receipt = auth.request_account_erasure_code(
        phone=phone,
        client_id="erasure-client",
        ip_address="203.0.113.20",
    )
    grant = auth.verify_phone_code(
        challenge_id=receipt.challenge_id,
        phone=phone,
        code=sms.last_code(),
        purpose=SmsPurpose.ACCOUNT_ERASURE,
    )
    auth.erase_qualifying_banned_account(phone=phone, grant=grant)


def test_three_distinct_accounts_block_phone_and_reputation_survives_each_erasure(
    tmp_path: Path,
) -> None:
    stack = make_auth_stack(tmp_path)
    actor = SanctionActor("admin", "admin-sanctions")
    phone = "+8613810000200"
    try:
        reputation_id: str | None = None
        for number in range(1, 4):
            user = make_test_account(stack, phone)
            result = stack.reputations.apply_qualifying_ban(
                user.user_id,
                operation_id=f"qualifying-ban-{number}",
                actor=actor,
                reason_code="terms_violation",
                now=stack.clock(),
            )
            reputation_id = reputation_id or result.phone_reputation_id
            assert result.phone_reputation_id == reputation_id
            assert result.qualifying_ban_count == number
            assert result.status == ("blocked" if number == 3 else "normal")
            repeated = stack.reputations.apply_qualifying_ban(
                user.user_id,
                operation_id=f"qualifying-ban-{number}",
                actor=actor,
                reason_code="terms_violation",
                now=stack.clock(),
            )
            assert repeated.qualifying_ban_count == number
            with pytest.raises(AccountStateError, match="already contributed"):
                stack.reputations.apply_qualifying_ban(
                    user.user_id,
                    operation_id=f"different-ban-{number}",
                    actor=actor,
                    reason_code="terms_violation",
                    now=stack.clock(),
                )
            _erase_banned_account(stack, phone)
            assert (
                stack.store.connection.execute(
                    "SELECT COUNT(*) FROM phone_reputations WHERE phone_reputation_id=?",
                    (reputation_id,),
                ).fetchone()[0]
                == 1
            )
        assert (
            stack.store.connection.execute(
                """SELECT COUNT(*) FROM phone_sanction_events
                WHERE event_type='qualifying_ban_applied'"""
            ).fetchone()[0]
            == 3
        )
    finally:
        stack.store.close()


def test_unblock_preserves_count_and_next_distinct_ban_blocks_again(tmp_path: Path) -> None:
    stack = make_auth_stack(tmp_path)
    actor = SanctionActor("admin", "admin-sanctions")
    phone = "+8613810000201"
    try:
        for number in range(1, 4):
            user = make_test_account(stack, phone)
            stack.reputations.apply_qualifying_ban(
                user.user_id,
                operation_id=f"ban-{number}",
                actor=actor,
                reason_code="terms_violation",
                now=stack.clock(),
            )
            _erase_banned_account(stack, phone)
        unblocked = stack.reputations.administrative_unblock_phone(
            phone,
            operation_id="unblock-1",
            actor=actor,
            reason_code="appeal_approved",
            now=stack.clock(),
        )
        assert unblocked.status == "normal"
        assert unblocked.qualifying_ban_count == 3
        fourth = make_test_account(stack, phone)
        blocked = stack.reputations.apply_qualifying_ban(
            fourth.user_id,
            operation_id="ban-4",
            actor=actor,
            reason_code="terms_violation",
            now=stack.clock(),
        )
        assert blocked.qualifying_ban_count == 4
        assert blocked.status == "blocked"
    finally:
        stack.store.close()


def test_overturn_is_unique_and_is_appended_to_the_original_generation(
    tmp_path: Path,
) -> None:
    stack = make_auth_stack(tmp_path)
    actor = SanctionActor("admin", "admin-sanctions")
    phone = "+8613810000202"
    try:
        user = make_test_account(stack, phone)
        stack.reputations.apply_qualifying_ban(
            user.user_id,
            operation_id="ban-original",
            actor=actor,
            reason_code="terms_violation",
            now=stack.clock(),
        )
        target = stack.store.connection.execute(
            """SELECT event_id FROM phone_sanction_events
            WHERE event_type='qualifying_ban_applied'"""
        ).fetchone()[0]
        overturned = stack.reputations.overturn_qualifying_ban(
            target,
            operation_id="overturn-original",
            actor=actor,
            reason_code="sanction_error",
            now=stack.clock(),
        )
        assert overturned.qualifying_ban_count == 0
        with pytest.raises(AccountStateError, match="already overturned"):
            stack.reputations.overturn_qualifying_ban(
                target,
                operation_id="overturn-again",
                actor=actor,
                reason_code="sanction_error",
                now=stack.clock(),
            )
        # The account is still suspended after overturn; remove it as an administrator for this
        # isolated reassignment precondition without touching the long-lived ledger.
        with stack.store.transaction() as tx:
            for table in (
                "access_tokens",
                "account_audit_events",
                "account_qualifying_bans",
                "sessions",
                "session_families",
                "password_credentials",
                "auth_identities",
                "users",
            ):
                tx.execute(f"DELETE FROM {table} WHERE user_id=?", (user.user_id,))
        reassigned = stack.reputations.reset_for_reassignment(
            phone,
            operation_id="reassignment-1",
            actor=actor,
            reason_code="carrier_reassignment_confirmed",
            now=stack.clock(),
        )
        assert reassigned.current_generation == 2
        assert (
            stack.store.connection.execute(
                """SELECT h.generation FROM phone_reputation_event_log h
            JOIN phone_sanction_events s ON s.event_id=h.event_id
            WHERE s.event_type='qualifying_ban_overturned' AND s.target_event_id=?""",
                (target,),
            ).fetchone()[0]
            == 1
        )
    finally:
        stack.store.close()


def test_common_head_sequence_and_head_detail_pairing_fail_closed(tmp_path: Path) -> None:
    stack = make_auth_stack(tmp_path)
    try:
        phone = "+8613810000203"
        make_test_account(stack, phone)
        reputation = stack.reputations.find_by_phone(phone)
        assert reputation is not None
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE constraint"):
            with stack.store.transaction() as tx:
                head = tx.execute(
                    """SELECT * FROM phone_reputation_event_log
                    WHERE phone_reputation_id=? AND generation=1 AND sequence_no=1""",
                    (reputation.phone_reputation_id,),
                ).fetchone()
                tx.execute(
                    """INSERT INTO phone_reputation_event_log VALUES (
                        'duplicate-sequence', ?, 1, 1, 'status', ?, 2,
                        'system', 'test', 'duplicate', 1
                    )""",
                    (reputation.phone_reputation_id, head["operation_id"]),
                )
        with stack.store.transaction() as tx:
            status_head = tx.execute(
                """SELECT event_id FROM phone_reputation_event_log
                WHERE phone_reputation_id=? AND stream_type='status' LIMIT 1""",
                (reputation.phone_reputation_id,),
            ).fetchone()[0]
            tx.execute("DELETE FROM phone_status_events WHERE event_id=?", (status_head,))
        with pytest.raises(PhoneReputationIntegrityError, match="exactly one matching detail"):
            stack.reputations.validate_one(reputation.phone_reputation_id)
    finally:
        stack.store.close()


def test_aead_tampering_fails_closed_instead_of_looking_like_no_reputation(
    tmp_path: Path,
) -> None:
    stack = make_auth_stack(tmp_path)
    try:
        phone = "+8613810000204"
        make_test_account(stack, phone)
        reputation = stack.reputations.find_by_phone(phone)
        assert reputation is not None
        with stack.store.transaction() as tx:
            tx.execute(
                """UPDATE phone_reputations SET encrypted_phone=?
                WHERE phone_reputation_id=?""",
                (b"tampered-ciphertext-with-tag", reputation.phone_reputation_id),
            )
        with pytest.raises(PhoneReputationIntegrityError, match="failed authentication"):
            stack.reputations.find_by_phone(phone)
    finally:
        stack.store.close()


@pytest.mark.parametrize(
    ("column", "message"),
    (
        ("reputation_key_id", "unknown stable lookup key"),
        ("phone_encryption_key_id", "unknown encryption key"),
    ),
)
def test_service_creation_fails_closed_when_database_references_unknown_key(
    tmp_path: Path, column: str, message: str
) -> None:
    stack = make_auth_stack(tmp_path)
    try:
        phone = "+8613810000205"
        make_test_account(stack, phone)
        with stack.store.transaction() as tx:
            tx.execute(f"UPDATE phone_reputations SET {column}='unknown-key'")
        fresh_crypto = PhoneReputationCrypto(
            stack.reputations.crypto.reputation_keys,
            stack.reputations.crypto.encryption_keys,
        )
        with pytest.raises(PhoneReputationIntegrityError, match=message):
            PhoneReputationService(stack.store, fresh_crypto)
        assert (
            stack.store.connection.execute("SELECT COUNT(*) FROM phone_reputations").fetchone()[0]
            == 1
        )
    finally:
        stack.store.close()


def test_keyring_preflight_runs_once_per_database_and_find_uses_only_exact_keys(
    tmp_path: Path,
) -> None:
    stack = make_auth_stack(tmp_path)
    try:
        phone = "+8613810000206"
        make_test_account(stack, phone)
        fresh_crypto = PhoneReputationCrypto(
            stack.reputations.crypto.reputation_keys,
            stack.reputations.crypto.encryption_keys,
        )
        statements: list[str] = []
        stack.store.connection.set_trace_callback(statements.append)
        first = PhoneReputationService(stack.store, fresh_crypto)
        PhoneReputationService(stack.store, fresh_crypto)
        assert sum("SELECT DISTINCT reputation_key_id" in sql for sql in statements) == 1

        statements.clear()
        assert first.find_by_phone(phone) is not None
        assert not any("SELECT DISTINCT reputation_key_id" in sql for sql in statements)
    finally:
        stack.store.connection.set_trace_callback(None)
        stack.store.close()


def test_find_by_phone_rejects_duplicate_identity_across_compatible_keys(tmp_path: Path) -> None:
    stack = make_auth_stack(tmp_path)
    try:
        phone = "+8613810000207"
        other_phone = "+8613810000208"
        make_test_account(stack, phone)
        make_test_account(stack, other_phone)
        other_reputation = stack.reputations.find_by_phone(other_phone)
        assert other_reputation is not None

        previous_key = SecretKey("reputation-previous", b"p" * 32)
        crypto = PhoneReputationCrypto(
            HmacKeyring(
                stack.reputations.crypto.reputation_keys.active,
                previous=(previous_key,),
            ),
            stack.reputations.crypto.encryption_keys,
        )
        service = PhoneReputationService(stack.store, crypto)
        key_id, phone_key = crypto.lookup_key(phone, key_id=previous_key.key_id)
        encryption_key_id, nonce, ciphertext = crypto.encrypt(
            other_reputation.phone_reputation_id, phone
        )
        with stack.store.transaction() as tx:
            tx.execute(
                """UPDATE phone_reputations
                SET reputation_key_id=?, phone_key=?, phone_encryption_key_id=?,
                    phone_encryption_nonce=?, encrypted_phone=?
                WHERE phone_reputation_id=?""",
                (
                    key_id,
                    phone_key,
                    encryption_key_id,
                    nonce,
                    ciphertext,
                    other_reputation.phone_reputation_id,
                ),
            )
        with pytest.raises(PhoneReputationIntegrityError, match="duplicate stable identities"):
            service.find_by_phone(phone)
    finally:
        stack.store.close()


def test_validate_one_bulk_loads_event_details_without_n_plus_one_queries(tmp_path: Path) -> None:
    stack = make_auth_stack(tmp_path)
    try:
        phone = "+8613810000209"
        user = make_test_account(stack, phone)
        reputation = stack.reputations.apply_qualifying_ban(
            user.user_id,
            operation_id="bulk-validation-ban",
            actor=SanctionActor("admin", "admin-bulk-validation"),
            reason_code="terms_violation",
            now=stack.clock(),
        )
        statements: list[str] = []
        stack.store.connection.set_trace_callback(statements.append)
        stack.reputations.validate_one(reputation.phone_reputation_id)
        normalized = [" ".join(sql.split()) for sql in statements]
        assert sum("FROM phone_sanction_events detail" in sql for sql in normalized) == 1
        assert sum("FROM phone_status_events detail" in sql for sql in normalized) == 1
        assert not any(
            "FROM phone_sanction_events WHERE event_id=" in sql
            or "FROM phone_status_events WHERE event_id=" in sql
            or "SELECT generation FROM phone_reputation_event_log WHERE event_id=" in sql
            for sql in normalized
        )
    finally:
        stack.store.connection.set_trace_callback(None)
        stack.store.close()
