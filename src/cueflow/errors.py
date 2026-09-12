class CueFlowError(Exception):
    """Base error for explicit CueFlow failures."""


class CancelledError(CueFlowError):
    """Cooperative stop; remote work and charges may remain."""

    metadata: object | None = None


class ContractError(CueFlowError):
    """A frozen schema or architecture invariant was violated."""

    def __init__(self, message: str, *, metadata: object | None = None) -> None:
        super().__init__(message)
        self.metadata = metadata


class IntegrityError(CueFlowError):
    """Persisted control-plane or artifact data is inconsistent."""


class SourceMissingError(IntegrityError):
    """A registered external source path is unavailable."""


class UnsupportedReferenceError(ContractError):
    """A Reference input is outside the explicit v0.5.4 contract."""


class ProviderError(CueFlowError):
    """A provider returned an explicit failure."""

    def __init__(self, message: str, *, metadata: object | None = None) -> None:
        super().__init__(message)
        self.metadata = metadata


class ProviderUnavailableError(ProviderError):
    """A provider runtime or credential is unavailable."""


class DeliveryAmbiguousError(ProviderError):
    """A remote request may have been delivered without a definite response."""


class ExportBlockedError(CueFlowError):
    """The export gate rejected the current project state."""


class SrtSerializationError(CueFlowError):
    """ATA values cannot be represented by the SRT serializer."""


class AccountError(CueFlowError):
    """Base error for the product-level Account Core."""


class AccountNotFoundError(AccountError):
    """The requested account object does not exist."""


class AccountStateError(AccountError):
    """The account state does not permit the requested transition."""


class IdentityConflictError(AccountError):
    """A verified identity is already active on another account."""


class SessionStateError(AccountError):
    """A Session or Session Family cannot perform the requested transition."""


class AccountMigrationError(AccountError):
    """The Account database could not reach the current forward-only schema."""


class AccountMigrationLockedError(AccountMigrationError):
    """Another process owns the Account database migration lock."""


class AuthenticationError(AccountError):
    """A public authentication attempt did not prove the requested identity."""


class AuthenticationRateLimitedError(AuthenticationError):
    """An authentication policy requires a temporary cooldown."""

    def __init__(self, message: str, *, retry_after_seconds: int) -> None:
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class PhoneBlockedError(AuthenticationError):
    """The supplied phone is explicitly blocked by Phone Reputation policy."""


class SmsProviderUnavailableError(AuthenticationError):
    """The SMS provider could not accept a verification request."""


class PhoneReputationIntegrityError(AccountError):
    """Long-lived Phone Reputation data failed closed integrity validation."""
