from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from cueflow.account_migrations import (
    ACCOUNT_SCHEMA_VERSION,
    read_account_schema_version,
    validate_account_invariants,
    validate_account_schema,
)
from cueflow.errors import AccountMigrationError, AccountNotFoundError, ContractError


class AccountStore:
    """Explicit product-level database; never created from a Workspace path."""

    def __init__(self, path: Path) -> None:
        if not path.is_absolute():
            raise AccountMigrationError("Account database path must be absolute")
        if not path.is_file():
            raise AccountMigrationError("Account database must be migrated before it is opened")
        self.path = path
        self.connection = sqlite3.connect(path)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA foreign_keys=ON")
            version = read_account_schema_version(self.connection)
            if version > ACCOUNT_SCHEMA_VERSION:
                raise AccountMigrationError("Account database is newer than this CueFlow build")
            if version < ACCOUNT_SCHEMA_VERSION:
                raise AccountMigrationError("Account database requires a forward migration")
            validate_account_schema(self.connection)
            validate_account_invariants(self.connection)
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA busy_timeout=5000")
        except BaseException:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self.connection.in_transaction:
            raise ContractError("nested AccountStore transactions are not supported")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def user(self, user_id: str, connection: sqlite3.Connection | None = None) -> sqlite3.Row:
        database = connection or self.connection
        row = database.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            raise AccountNotFoundError("unknown User")
        return cast(sqlite3.Row, row)

    def identity(
        self, identity_id: str, connection: sqlite3.Connection | None = None
    ) -> sqlite3.Row:
        database = connection or self.connection
        row = database.execute(
            "SELECT * FROM auth_identities WHERE identity_id=?", (identity_id,)
        ).fetchone()
        if row is None:
            raise AccountNotFoundError("unknown AuthIdentity")
        return cast(sqlite3.Row, row)

    def session(
        self, session_id: str, connection: sqlite3.Connection | None = None
    ) -> sqlite3.Row:
        database = connection or self.connection
        row = database.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if row is None:
            raise AccountNotFoundError("unknown Session")
        return cast(sqlite3.Row, row)

    def session_family(
        self, session_family_id: str, connection: sqlite3.Connection | None = None
    ) -> sqlite3.Row:
        database = connection or self.connection
        row = database.execute(
            "SELECT * FROM session_families WHERE session_family_id=?",
            (session_family_id,),
        ).fetchone()
        if row is None:
            raise AccountNotFoundError("unknown SessionFamily")
        return cast(sqlite3.Row, row)
