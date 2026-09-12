from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from typing import cast

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from cueflow.account_store import AccountStore
from cueflow.auth_crypto import HmacKeyring, SecretKey, request_fingerprint
from cueflow.errors import (
    AccountNotFoundError,
    AccountStateError,
    ContractError,
    PhoneBlockedError,
    PhoneReputationIntegrityError,
)

_E164 = re.compile(r"^\+[1-9][0-9]{1,14}$")
_AAD_PREFIX = b"cueflow:phone-reputation:v1:"


@dataclass(frozen=True)
class AeadKeyring:
    active: SecretKey
    previous: tuple[SecretKey, ...] = ()
    _keys: dict[str, SecretKey] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        values = (self.active, *self.previous)
        if any(len(item.value) != 32 for item in values):
            raise ContractError("Phone Reputation AES keys must contain exactly 256 bits")
        keys = {item.key_id: item for item in values}
        if len(keys) != len(values):
            raise ContractError("Phone Reputation AES key ids must be unique")
        object.__setattr__(self, "_keys", keys)

    def key(self, key_id: str) -> SecretKey:
        try:
            return self._keys[key_id]
        except KeyError as exc:
            raise PhoneReputationIntegrityError(
                "Phone Reputation references an unknown encryption key"
            ) from exc


@dataclass(frozen=True)
class PhoneReputationCrypto:
    reputation_keys: HmacKeyring
    encryption_keys: AeadKeyring

    def lookup_key(self, phone: str, *, key_id: str | None = None) -> tuple[str, bytes]:
        _validate_e164(phone)
        key = self.reputation_keys.active if key_id is None else self.reputation_keys.key(key_id)
        return key.key_id, self.reputation_keys.digest(
            "phone-reputation-lookup-v1", phone.encode("ascii"), key_id=key.key_id
        )

    def encrypt(self, phone_reputation_id: str, phone: str) -> tuple[str, bytes, bytes]:
        _validate_e164(phone)
        key = self.encryption_keys.active
        nonce = os.urandom(12)
        ciphertext = AESGCM(key.value).encrypt(
            nonce,
            phone.encode("ascii"),
            _AAD_PREFIX + phone_reputation_id.encode("ascii"),
        )
        return key.key_id, nonce, ciphertext

    def decrypt(self, row: sqlite3.Row) -> str:
        key = self.encryption_keys.key(str(row["phone_encryption_key_id"]))
        try:
            plaintext = AESGCM(key.value).decrypt(
                bytes(row["phone_encryption_nonce"]),
                bytes(row["encrypted_phone"]),
                _AAD_PREFIX + str(row["phone_reputation_id"]).encode("ascii"),
            )
            phone = plaintext.decode("ascii")
        except (InvalidTag, UnicodeDecodeError, ValueError) as exc:
            raise PhoneReputationIntegrityError(
                "Phone Reputation encrypted phone failed authentication"
            ) from exc
        _validate_e164(phone)
        key_id, expected = self.lookup_key(phone, key_id=str(row["reputation_key_id"]))
        if key_id != row["reputation_key_id"] or not hmac.compare_digest(
            expected, bytes(row["phone_key"])
        ):
            raise PhoneReputationIntegrityError(
                "Phone Reputation encrypted phone does not match its stable lookup key"
            )
        return phone


@dataclass(frozen=True)
class PhoneReputation:
    phone_reputation_id: str
    phone: str = field(repr=False)
    current_generation: int
    qualifying_ban_count: int
    status: str
    blocked_at: int | None
    last_ban_at: int | None


@dataclass(frozen=True)
class SanctionActor:
    actor_type: str
    actor_id: str

    def __post_init__(self) -> None:
        if self.actor_type not in {"system", "admin", "service"}:
            raise ContractError("sanction actor type is invalid")
        if not self.actor_id or len(self.actor_id.encode("utf-8")) > 128:
            raise ContractError("sanction actor id is invalid")


class PhoneReputationService:
    def __init__(self, store: AccountStore, crypto: PhoneReputationCrypto) -> None:
        self.store = store
        self.crypto = crypto

    def find_by_phone(
        self, phone: str, *, connection: sqlite3.Connection | None = None
    ) -> PhoneReputation | None:
        tx = connection or self.store.connection
        stored_key_ids = {
            str(row[0])
            for row in tx.execute(
                "SELECT DISTINCT reputation_key_id FROM phone_reputations"
            ).fetchall()
        }
        unknown_key_ids = stored_key_ids - set(self.crypto.reputation_keys.key_ids)
        if unknown_key_ids:
            raise PhoneReputationIntegrityError(
                "Phone Reputation references an unknown stable lookup key"
            )
        matches: list[sqlite3.Row] = []
        for key_id in self.crypto.reputation_keys.key_ids:
            _, phone_key = self.crypto.lookup_key(phone, key_id=key_id)
            row = tx.execute(
                "SELECT * FROM phone_reputations WHERE reputation_key_id=? AND phone_key=?",
                (key_id, phone_key),
            ).fetchone()
            if row is not None:
                matches.append(cast(sqlite3.Row, row))
        if not matches:
            return None
        if len(matches) != 1:
            raise PhoneReputationIntegrityError(
                "Phone Reputation contains duplicate stable identities across lookup keys"
            )
        row = matches[0]
        self.validate_one(str(row["phone_reputation_id"]), connection=tx)
        return self._value(row)

    def require_normal(
        self, phone: str, *, connection: sqlite3.Connection | None = None
    ) -> PhoneReputation | None:
        reputation = self.find_by_phone(phone, connection=connection)
        if reputation is not None and reputation.status == "blocked":
            raise PhoneBlockedError("phone is blocked; contact support to appeal")
        return reputation

    def ensure_phone_tx(self, tx: sqlite3.Connection, phone: str, *, now: int) -> PhoneReputation:
        existing = self.find_by_phone(phone, connection=tx)
        if existing is not None:
            return existing
        reputation_id = "prp_" + uuid.uuid4().hex
        reputation_key_id, phone_key = self.crypto.lookup_key(phone)
        encryption_key_id, nonce, ciphertext = self.crypto.encrypt(reputation_id, phone)
        tx.execute(
            """INSERT INTO phone_reputations VALUES (
                ?, ?, ?, ?, ?, ?, 1, 0, 'normal', NULL, NULL, ?, ?
            )""",
            (
                reputation_id,
                reputation_key_id,
                phone_key,
                encryption_key_id,
                nonce,
                ciphertext,
                now,
                now,
            ),
        )
        operation_id = "pop_" + uuid.uuid4().hex
        actor = SanctionActor("system", "cueflow-auth")
        self._insert_operation_tx(
            tx,
            operation_id=operation_id,
            reputation_id=reputation_id,
            generation=1,
            operation_type="generation_started",
            fingerprint=request_fingerprint(reputation_id, "1", "generation_started"),
            expected_sanction=1,
            expected_status=1,
            result_code="generation_started",
            actor=actor,
            reason_code="first_registration",
            now=now,
        )
        status_id = self._append_head_tx(
            tx,
            reputation_id=reputation_id,
            generation=1,
            sequence_no=1,
            stream_type="status",
            operation_id=operation_id,
            actor=actor,
            reason_code="first_registration",
            now=now,
        )
        tx.execute(
            """INSERT INTO phone_status_events
            VALUES (?, 'generation_started', NULL, 'normal', NULL)""",
            (status_id,),
        )
        sanction_id = self._append_head_tx(
            tx,
            reputation_id=reputation_id,
            generation=1,
            sequence_no=2,
            stream_type="sanction",
            operation_id=operation_id,
            actor=actor,
            reason_code="first_registration",
            now=now,
        )
        tx.execute(
            "INSERT INTO phone_sanction_events VALUES (?, 'generation_started', 0, NULL)",
            (sanction_id,),
        )
        row = tx.execute(
            "SELECT * FROM phone_reputations WHERE phone_reputation_id=?", (reputation_id,)
        ).fetchone()
        assert row is not None
        return self._value(cast(sqlite3.Row, row))

    def administrative_block_phone(
        self,
        phone: str,
        *,
        operation_id: str,
        actor: SanctionActor,
        reason_code: str,
        now: int,
    ) -> PhoneReputation:
        with self.store.transaction() as tx:
            row = self._require_row_by_phone_tx(tx, phone)
            generation = int(row["current_generation"])
            fingerprint = request_fingerprint(
                str(row["phone_reputation_id"]), "administrative_block", reason_code
            )
            existing = self._existing_operation_tx(tx, operation_id, fingerprint)
            if existing is not None:
                self.validate_one(str(row["phone_reputation_id"]), connection=tx)
                return self._value(row)
            if row["status"] == "blocked":
                self._insert_operation_tx(
                    tx,
                    operation_id=operation_id,
                    reputation_id=str(row["phone_reputation_id"]),
                    generation=generation,
                    operation_type="administrative_block",
                    fingerprint=fingerprint,
                    expected_sanction=0,
                    expected_status=0,
                    result_code="already_blocked",
                    actor=actor,
                    reason_code=reason_code,
                    now=now,
                )
                return self._value(row)
            self._insert_operation_tx(
                tx,
                operation_id=operation_id,
                reputation_id=str(row["phone_reputation_id"]),
                generation=generation,
                operation_type="administrative_block",
                fingerprint=fingerprint,
                expected_sanction=0,
                expected_status=1,
                result_code="phone_blocked",
                actor=actor,
                reason_code=reason_code,
                now=now,
            )
            event_id = self._append_next_head_tx(
                tx, row, "status", operation_id, actor, reason_code, now
            )
            tx.execute(
                """INSERT INTO phone_status_events
                VALUES (?, 'phone_blocked', 'normal', 'blocked', NULL)""",
                (event_id,),
            )
            tx.execute(
                """UPDATE phone_reputations
                SET status='blocked', blocked_at=?, updated_at=?
                WHERE phone_reputation_id=?""",
                (now, now, row["phone_reputation_id"]),
            )
            self._revoke_phone_sessions_tx(tx, phone, now, "administrative_revoke")
            return self._reload_value(tx, str(row["phone_reputation_id"]))

    def administrative_unblock_phone(
        self,
        phone: str,
        *,
        operation_id: str,
        actor: SanctionActor,
        reason_code: str,
        now: int,
    ) -> PhoneReputation:
        with self.store.transaction() as tx:
            row = self._require_row_by_phone_tx(tx, phone)
            fingerprint = request_fingerprint(
                str(row["phone_reputation_id"]), "administrative_unblock", reason_code
            )
            existing = self._existing_operation_tx(tx, operation_id, fingerprint)
            if existing is not None:
                self.validate_one(str(row["phone_reputation_id"]), connection=tx)
                return self._value(row)
            expected_status = 1 if row["status"] == "blocked" else 0
            result = "phone_unblocked" if expected_status else "already_normal"
            self._insert_operation_tx(
                tx,
                operation_id=operation_id,
                reputation_id=str(row["phone_reputation_id"]),
                generation=int(row["current_generation"]),
                operation_type="administrative_unblock",
                fingerprint=fingerprint,
                expected_sanction=0,
                expected_status=expected_status,
                result_code=result,
                actor=actor,
                reason_code=reason_code,
                now=now,
            )
            if expected_status:
                event_id = self._append_next_head_tx(
                    tx, row, "status", operation_id, actor, reason_code, now
                )
                tx.execute(
                    """INSERT INTO phone_status_events
                    VALUES (?, 'phone_unblocked', 'blocked', 'normal', NULL)""",
                    (event_id,),
                )
                tx.execute(
                    """UPDATE phone_reputations
                    SET status='normal', blocked_at=NULL, updated_at=?
                    WHERE phone_reputation_id=?""",
                    (now, row["phone_reputation_id"]),
                )
            return self._reload_value(tx, str(row["phone_reputation_id"]))

    def apply_qualifying_ban(
        self,
        user_id: str,
        *,
        operation_id: str,
        actor: SanctionActor,
        reason_code: str,
        now: int,
    ) -> PhoneReputation:
        with self.store.transaction() as tx:
            identity = tx.execute(
                """SELECT provider_subject FROM auth_identities
                WHERE user_id=? AND provider='phone' AND status='active'""",
                (user_id,),
            ).fetchone()
            if identity is None:
                raise AccountStateError("User does not have exactly one active phone")
            phone = str(identity["provider_subject"])
            row = self._require_row_by_phone_tx(tx, phone)
            fingerprint = request_fingerprint(
                user_id, str(row["phone_reputation_id"]), "qualifying_ban", reason_code
            )
            existing = self._existing_operation_tx(tx, operation_id, fingerprint)
            if existing is not None:
                self.validate_one(str(row["phone_reputation_id"]), connection=tx)
                return self._value(row)
            bridge = tx.execute(
                "SELECT 1 FROM account_qualifying_bans WHERE user_id=? AND overturned_at IS NULL",
                (user_id,),
            ).fetchone()
            if bridge is not None:
                raise AccountStateError("User already contributed a qualifying ban")
            new_count = int(row["qualifying_ban_count"]) + 1
            should_block = row["status"] == "normal" and new_count >= 3
            self._insert_operation_tx(
                tx,
                operation_id=operation_id,
                reputation_id=str(row["phone_reputation_id"]),
                generation=int(row["current_generation"]),
                operation_type="qualifying_ban",
                fingerprint=fingerprint,
                expected_sanction=1,
                expected_status=int(should_block),
                result_code="phone_blocked" if should_block else "qualifying_ban_applied",
                actor=actor,
                reason_code=reason_code,
                now=now,
            )
            sanction_id = self._append_next_head_tx(
                tx, row, "sanction", operation_id, actor, reason_code, now
            )
            tx.execute(
                "INSERT INTO phone_sanction_events VALUES (?, 'qualifying_ban_applied', 1, NULL)",
                (sanction_id,),
            )
            if should_block:
                status_id = self._append_next_head_tx(
                    tx, row, "status", operation_id, actor, reason_code, now
                )
                tx.execute(
                    """INSERT INTO phone_status_events
                    VALUES (?, 'phone_blocked', 'normal', 'blocked', ?)""",
                    (status_id, sanction_id),
                )
            tx.execute(
                """UPDATE phone_reputations
                SET qualifying_ban_count=?, status=?, blocked_at=?, last_ban_at=?, updated_at=?
                WHERE phone_reputation_id=?""",
                (
                    new_count,
                    "blocked" if should_block else row["status"],
                    now if should_block else row["blocked_at"],
                    now,
                    now,
                    row["phone_reputation_id"],
                ),
            )
            tx.execute(
                "INSERT INTO account_qualifying_bans VALUES (?, ?, ?, ?, ?, NULL)",
                (
                    "aqb_" + uuid.uuid4().hex,
                    user_id,
                    row["phone_reputation_id"],
                    sanction_id,
                    now,
                ),
            )
            tx.execute(
                "UPDATE users SET status='suspended', updated_at=? WHERE user_id=?", (now, user_id)
            )
            self._revoke_user_sessions_tx(tx, user_id, now, "user_suspended")
            _account_audit(tx, user_id, "qualifying_ban_applied", sanction_id, reason_code, now)
            return self._reload_value(tx, str(row["phone_reputation_id"]))

    def overturn_qualifying_ban(
        self,
        target_event_id: str,
        *,
        operation_id: str,
        actor: SanctionActor,
        reason_code: str,
        now: int,
    ) -> PhoneReputation:
        with self.store.transaction() as tx:
            target = tx.execute(
                """SELECT h.*, s.event_type FROM phone_reputation_event_log h
                JOIN phone_sanction_events s ON s.event_id=h.event_id
                WHERE h.event_id=?""",
                (target_event_id,),
            ).fetchone()
            if target is None or target["event_type"] != "qualifying_ban_applied":
                raise AccountNotFoundError("unknown qualifying ban sanction event")
            reputation_id = str(target["phone_reputation_id"])
            row = self._row_by_id_tx(tx, reputation_id)
            fingerprint = request_fingerprint(target_event_id, "overturn", reason_code)
            existing = self._existing_operation_tx(tx, operation_id, fingerprint)
            if existing is not None:
                self.validate_one(reputation_id, connection=tx)
                return self._value(row)
            if tx.execute(
                """SELECT 1 FROM phone_sanction_events
                WHERE event_type='qualifying_ban_overturned' AND target_event_id=?""",
                (target_event_id,),
            ).fetchone():
                raise AccountStateError("qualifying ban was already overturned")
            generation = int(target["generation"])
            latest_status = tx.execute(
                """SELECT h.sequence_no, s.* FROM phone_reputation_event_log h
                JOIN phone_status_events s ON s.event_id=h.event_id
                WHERE h.phone_reputation_id=? AND h.generation=?
                ORDER BY h.sequence_no DESC LIMIT 1""",
                (reputation_id, generation),
            ).fetchone()
            should_unblock = (
                generation == int(row["current_generation"])
                and latest_status is not None
                and latest_status["event_type"] == "phone_blocked"
                and latest_status["caused_by_sanction_event_id"] == target_event_id
            )
            self._insert_operation_tx(
                tx,
                operation_id=operation_id,
                reputation_id=reputation_id,
                generation=generation,
                operation_type="overturn_sanction",
                fingerprint=fingerprint,
                expected_sanction=1,
                expected_status=int(should_unblock),
                result_code="qualifying_ban_overturned",
                actor=actor,
                reason_code=reason_code,
                now=now,
            )
            sanction_id = self._append_next_head_for_generation_tx(
                tx,
                reputation_id,
                generation,
                "sanction",
                operation_id,
                actor,
                reason_code,
                now,
            )
            tx.execute(
                "INSERT INTO phone_sanction_events VALUES (?, 'qualifying_ban_overturned', -1, ?)",
                (sanction_id, target_event_id),
            )
            if should_unblock:
                status_id = self._append_next_head_for_generation_tx(
                    tx,
                    reputation_id,
                    generation,
                    "status",
                    operation_id,
                    actor,
                    reason_code,
                    now,
                )
                tx.execute(
                    """INSERT INTO phone_status_events
                    VALUES (?, 'phone_unblocked', 'blocked', 'normal', ?)""",
                    (status_id, target_event_id),
                )
            tx.execute(
                """UPDATE account_qualifying_bans SET overturned_at=?
                WHERE sanction_event_id=? AND overturned_at IS NULL""",
                (now, target_event_id),
            )
            if generation == int(row["current_generation"]):
                tx.execute(
                    """UPDATE phone_reputations
                    SET qualifying_ban_count=qualifying_ban_count-1,
                        status=?, blocked_at=?, updated_at=?
                    WHERE phone_reputation_id=?""",
                    (
                        "normal" if should_unblock else row["status"],
                        None if should_unblock else row["blocked_at"],
                        now,
                        reputation_id,
                    ),
                )
            return self._reload_value(tx, reputation_id)

    def reset_for_reassignment(
        self,
        phone: str,
        *,
        operation_id: str,
        actor: SanctionActor,
        reason_code: str,
        now: int,
    ) -> PhoneReputation:
        with self.store.transaction() as tx:
            if tx.execute(
                """SELECT 1 FROM auth_identities
                WHERE provider='phone' AND provider_subject=? AND status='active'""",
                (phone,),
            ).fetchone():
                raise AccountStateError("phone reassignment requires no active account identity")
            row = self._require_row_by_phone_tx(tx, phone)
            fingerprint = request_fingerprint(
                str(row["phone_reputation_id"]), "phone_reassignment", reason_code
            )
            existing = self._existing_operation_tx(tx, operation_id, fingerprint)
            if existing is not None:
                self.validate_one(str(row["phone_reputation_id"]), connection=tx)
                return self._value(row)
            generation = int(row["current_generation"]) + 1
            self._insert_operation_tx(
                tx,
                operation_id=operation_id,
                reputation_id=str(row["phone_reputation_id"]),
                generation=generation,
                operation_type="phone_reassignment",
                fingerprint=fingerprint,
                expected_sanction=1,
                expected_status=1,
                result_code="generation_started",
                actor=actor,
                reason_code=reason_code,
                now=now,
            )
            status_id = self._append_head_tx(
                tx,
                reputation_id=str(row["phone_reputation_id"]),
                generation=generation,
                sequence_no=1,
                stream_type="status",
                operation_id=operation_id,
                actor=actor,
                reason_code=reason_code,
                now=now,
            )
            tx.execute(
                """INSERT INTO phone_status_events
                VALUES (?, 'generation_started', NULL, 'normal', NULL)""",
                (status_id,),
            )
            sanction_id = self._append_head_tx(
                tx,
                reputation_id=str(row["phone_reputation_id"]),
                generation=generation,
                sequence_no=2,
                stream_type="sanction",
                operation_id=operation_id,
                actor=actor,
                reason_code=reason_code,
                now=now,
            )
            tx.execute(
                "INSERT INTO phone_sanction_events VALUES (?, 'generation_started', 0, NULL)",
                (sanction_id,),
            )
            tx.execute(
                """UPDATE phone_reputations SET current_generation=?, qualifying_ban_count=0,
                status='normal', blocked_at=NULL, last_ban_at=NULL, updated_at=?
                WHERE phone_reputation_id=?""",
                (generation, now, row["phone_reputation_id"]),
            )
            return self._reload_value(tx, str(row["phone_reputation_id"]))

    def validate_one(
        self, phone_reputation_id: str, *, connection: sqlite3.Connection | None = None
    ) -> None:
        tx = connection or self.store.connection
        row = self._row_by_id_tx(tx, phone_reputation_id)
        self.crypto.decrypt(row)
        operations = tx.execute(
            """SELECT * FROM phone_reputation_operations
            WHERE phone_reputation_id=? ORDER BY generation, created_at, operation_id""",
            (phone_reputation_id,),
        ).fetchall()
        heads = tx.execute(
            """SELECT * FROM phone_reputation_event_log
            WHERE phone_reputation_id=? ORDER BY generation, sequence_no""",
            (phone_reputation_id,),
        ).fetchall()
        by_operation: dict[str, list[sqlite3.Row]] = {}
        generation_heads: dict[int, list[sqlite3.Row]] = {}
        for head in heads:
            generation_heads.setdefault(int(head["generation"]), []).append(head)
            by_operation.setdefault(str(head["operation_id"]), []).append(head)
            sanction = tx.execute(
                "SELECT * FROM phone_sanction_events WHERE event_id=?", (head["event_id"],)
            ).fetchone()
            status = tx.execute(
                "SELECT * FROM phone_status_events WHERE event_id=?", (head["event_id"],)
            ).fetchone()
            if head["stream_type"] == "sanction":
                valid_pair = sanction is not None and status is None
            else:
                valid_pair = status is not None and sanction is None
            if not valid_pair:
                raise PhoneReputationIntegrityError(
                    "Phone Reputation event head does not have exactly one matching detail"
                )
        for operation in operations:
            operation_heads = by_operation.pop(str(operation["operation_id"]), [])
            sanctions = sum(head["stream_type"] == "sanction" for head in operation_heads)
            statuses = sum(head["stream_type"] == "status" for head in operation_heads)
            if sanctions != int(operation["expected_sanction_events"]) or statuses != int(
                operation["expected_status_events"]
            ):
                raise PhoneReputationIntegrityError(
                    "Phone Reputation operation does not contain its complete event streams"
                )
            for head in operation_heads:
                if (
                    head["phone_reputation_id"] != operation["phone_reputation_id"]
                    or head["generation"] != operation["generation"]
                    or head["actor_type"] != operation["actor_type"]
                    or head["actor_id"] != operation["actor_id"]
                    or head["reason_code"] != operation["reason_code"]
                ):
                    raise PhoneReputationIntegrityError(
                        "Phone Reputation operation and event head disagree"
                    )
        if by_operation:
            raise PhoneReputationIntegrityError(
                "Phone Reputation event references an operation outside its ledger"
            )
        current_count = -1
        current_status = ""
        current_blocked_at: int | None = None
        current_last_ban_at: int | None = None
        for generation, items in sorted(generation_heads.items()):
            if generation > int(row["current_generation"]):
                raise PhoneReputationIntegrityError("Phone Reputation contains a future generation")
            if [int(item["sequence_no"]) for item in items] != list(range(1, len(items) + 1)):
                raise PhoneReputationIntegrityError(
                    "Phone Reputation generation sequence is not contiguous"
                )
            count = 0
            status_value = "normal"
            blocked_at: int | None = None
            last_ban_at: int | None = None
            seen_applied: set[str] = set()
            seen_overturned: set[str] = set()
            for index, head in enumerate(items):
                if head["stream_type"] == "sanction":
                    detail = tx.execute(
                        "SELECT * FROM phone_sanction_events WHERE event_id=?",
                        (head["event_id"],),
                    ).fetchone()
                    assert detail is not None
                    event_type = str(detail["event_type"])
                    if event_type == "generation_started":
                        if index != 1:
                            raise PhoneReputationIntegrityError(
                                "sanction generation start is not sequence 2"
                            )
                    elif event_type == "qualifying_ban_applied":
                        count += 1
                        last_ban_at = int(head["created_at"])
                        seen_applied.add(str(head["event_id"]))
                    else:
                        target = str(detail["target_event_id"])
                        target_head = tx.execute(
                            "SELECT generation FROM phone_reputation_event_log WHERE event_id=?",
                            (target,),
                        ).fetchone()
                        if (
                            target not in seen_applied
                            or target in seen_overturned
                            or target_head is None
                            or int(target_head["generation"]) != generation
                        ):
                            raise PhoneReputationIntegrityError(
                                "qualifying ban overturn does not target one prior ban "
                                "in its generation"
                            )
                        count -= 1
                        seen_overturned.add(target)
                    if count < 0:
                        raise PhoneReputationIntegrityError(
                            "Phone Reputation sanction count became negative"
                        )
                else:
                    detail = tx.execute(
                        "SELECT * FROM phone_status_events WHERE event_id=?", (head["event_id"],)
                    ).fetchone()
                    assert detail is not None
                    event_type = str(detail["event_type"])
                    if event_type == "generation_started":
                        if index != 0:
                            raise PhoneReputationIntegrityError(
                                "status generation start is not sequence 1"
                            )
                    elif detail["from_status"] != status_value:
                        raise PhoneReputationIntegrityError(
                            "Phone Reputation status transition does not match prior status"
                        )
                    else:
                        status_value = str(detail["to_status"])
                        blocked_at = int(head["created_at"]) if status_value == "blocked" else None
            if generation == int(row["current_generation"]):
                current_count = count
                current_status = status_value
                current_blocked_at = blocked_at
                current_last_ban_at = last_ban_at
        if current_count < 0:
            raise PhoneReputationIntegrityError(
                "Phone Reputation current generation has no event ledger"
            )
        if (
            current_count != int(row["qualifying_ban_count"])
            or current_status != row["status"]
            or current_blocked_at != row["blocked_at"]
            or current_last_ban_at != row["last_ban_at"]
        ):
            raise PhoneReputationIntegrityError(
                "Phone Reputation projection does not match its event streams"
            )

    def validate_all(self) -> int:
        rows = self.store.connection.execute(
            "SELECT phone_reputation_id FROM phone_reputations ORDER BY phone_reputation_id"
        ).fetchall()
        for row in rows:
            self.validate_one(str(row["phone_reputation_id"]))
        return len(rows)

    def _require_row_by_phone_tx(self, tx: sqlite3.Connection, phone: str) -> sqlite3.Row:
        reputation = self.find_by_phone(phone, connection=tx)
        if reputation is None:
            raise AccountNotFoundError("unknown Phone Reputation")
        return self._row_by_id_tx(tx, reputation.phone_reputation_id)

    @staticmethod
    def _row_by_id_tx(tx: sqlite3.Connection, reputation_id: str) -> sqlite3.Row:
        row = tx.execute(
            "SELECT * FROM phone_reputations WHERE phone_reputation_id=?", (reputation_id,)
        ).fetchone()
        if row is None:
            raise PhoneReputationIntegrityError("Phone Reputation row is missing")
        return cast(sqlite3.Row, row)

    def _reload_value(self, tx: sqlite3.Connection, reputation_id: str) -> PhoneReputation:
        self.validate_one(reputation_id, connection=tx)
        return self._value(self._row_by_id_tx(tx, reputation_id))

    def _value(self, row: sqlite3.Row) -> PhoneReputation:
        return PhoneReputation(
            str(row["phone_reputation_id"]),
            self.crypto.decrypt(row),
            int(row["current_generation"]),
            int(row["qualifying_ban_count"]),
            str(row["status"]),
            None if row["blocked_at"] is None else int(row["blocked_at"]),
            None if row["last_ban_at"] is None else int(row["last_ban_at"]),
        )

    @staticmethod
    def _existing_operation_tx(
        tx: sqlite3.Connection, operation_id: str, fingerprint: bytes
    ) -> sqlite3.Row | None:
        row = tx.execute(
            "SELECT * FROM phone_reputation_operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is not None and not hmac.compare_digest(
            bytes(row["request_fingerprint"]), fingerprint
        ):
            raise ContractError("Phone Reputation operation id was reused for another request")
        return cast(sqlite3.Row | None, row)

    @staticmethod
    def _insert_operation_tx(
        tx: sqlite3.Connection,
        *,
        operation_id: str,
        reputation_id: str,
        generation: int,
        operation_type: str,
        fingerprint: bytes,
        expected_sanction: int,
        expected_status: int,
        result_code: str,
        actor: SanctionActor,
        reason_code: str,
        now: int,
    ) -> None:
        if not reason_code or len(reason_code.encode("utf-8")) > 128:
            raise ContractError("Phone Reputation reason code is invalid")
        tx.execute(
            "INSERT INTO phone_reputation_operations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                operation_id,
                reputation_id,
                generation,
                operation_type,
                fingerprint,
                expected_sanction,
                expected_status,
                result_code,
                actor.actor_type,
                actor.actor_id,
                reason_code,
                now,
            ),
        )

    def _append_next_head_tx(
        self,
        tx: sqlite3.Connection,
        reputation: sqlite3.Row,
        stream_type: str,
        operation_id: str,
        actor: SanctionActor,
        reason_code: str,
        now: int,
    ) -> str:
        return self._append_next_head_for_generation_tx(
            tx,
            str(reputation["phone_reputation_id"]),
            int(reputation["current_generation"]),
            stream_type,
            operation_id,
            actor,
            reason_code,
            now,
        )

    def _append_next_head_for_generation_tx(
        self,
        tx: sqlite3.Connection,
        reputation_id: str,
        generation: int,
        stream_type: str,
        operation_id: str,
        actor: SanctionActor,
        reason_code: str,
        now: int,
    ) -> str:
        row = tx.execute(
            """SELECT COALESCE(MAX(sequence_no), 0) + 1
            FROM phone_reputation_event_log
            WHERE phone_reputation_id=? AND generation=?""",
            (reputation_id, generation),
        ).fetchone()
        assert row is not None
        return self._append_head_tx(
            tx,
            reputation_id=reputation_id,
            generation=generation,
            sequence_no=int(row[0]),
            stream_type=stream_type,
            operation_id=operation_id,
            actor=actor,
            reason_code=reason_code,
            now=now,
        )

    @staticmethod
    def _append_head_tx(
        tx: sqlite3.Connection,
        *,
        reputation_id: str,
        generation: int,
        sequence_no: int,
        stream_type: str,
        operation_id: str,
        actor: SanctionActor,
        reason_code: str,
        now: int,
    ) -> str:
        event_id = "pse_" + uuid.uuid4().hex
        tx.execute(
            "INSERT INTO phone_reputation_event_log VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                reputation_id,
                generation,
                sequence_no,
                stream_type,
                operation_id,
                1 if stream_type == "sanction" else 2,
                actor.actor_type,
                actor.actor_id,
                reason_code,
                now,
            ),
        )
        return event_id

    @staticmethod
    def _revoke_user_sessions_tx(
        tx: sqlite3.Connection, user_id: str, now: int, reason: str
    ) -> None:
        tx.execute(
            """UPDATE session_families SET revoked_at=?, revocation_reason=?
            WHERE user_id=? AND revoked_at IS NULL""",
            (now, reason, user_id),
        )
        tx.execute(
            "UPDATE sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (now, user_id),
        )
        tx.execute(
            "UPDATE access_tokens SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
            (now, user_id),
        )

    def _revoke_phone_sessions_tx(
        self, tx: sqlite3.Connection, phone: str, now: int, reason: str
    ) -> None:
        rows = tx.execute(
            """SELECT user_id FROM auth_identities
            WHERE provider='phone' AND provider_subject=? AND status='active'""",
            (phone,),
        ).fetchall()
        for row in rows:
            user_id = str(row["user_id"])
            self._revoke_user_sessions_tx(tx, user_id, now, reason)
            _account_audit(tx, user_id, "phone_blocked", None, reason, now)


def _validate_e164(phone: str) -> None:
    if not _E164.fullmatch(phone):
        raise ContractError("phone must be canonical E.164")


def _account_audit(
    tx: sqlite3.Connection,
    user_id: str,
    event_type: str,
    subject_id: str | None,
    reason_code: str,
    now: int,
) -> None:
    tx.execute(
        "INSERT INTO account_audit_events VALUES (?, ?, ?, ?, ?, ?)",
        (
            "evt_" + uuid.uuid4().hex,
            user_id,
            event_type,
            subject_id,
            json.dumps({"reason": reason_code}, sort_keys=True, separators=(",", ":")),
            now,
        ),
    )
