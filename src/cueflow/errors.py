class CueFlowError(Exception):
    """Base error for explicit CueFlow failures."""


class ContractError(CueFlowError):
    """A frozen schema or architecture invariant was violated."""


class IntegrityError(CueFlowError):
    """Persisted control-plane or artifact data is inconsistent."""


class SourceMissingError(IntegrityError):
    """The registered external source path is unavailable."""


class ReferenceMissingError(IntegrityError):
    """A registered Reference locator is missing or unreadable."""


class UnsupportedReferenceError(ContractError):
    """A Reference format or route is explicitly unsupported."""


class ProviderError(CueFlowError):
    """Base provider failure."""


class ProviderUnavailableError(ProviderError):
    """A required provider runtime or credential is unavailable."""


class DeliveryAmbiguousError(ProviderError):
    """A remote request may have been delivered but no definite result was received."""


class ProviderIdentityError(ProviderError):
    """Provider credentials are missing or rejected."""


class ProviderPermissionError(ProviderError):
    """Provider credentials do not grant the requested operation."""


class ProviderFormatError(ProviderError):
    """The provider rejected the supplied Reference format."""


class ProviderCleanupError(ProviderError):
    """A provider-owned temporary resource could not be deleted."""


class ReferenceRunFailedError(CueFlowError):
    """A Reference Run reached the frozen partial or failed outcome."""

    def __init__(self, run_id: str, outcome: str) -> None:
        super().__init__(f"Reference Run {run_id} finished with outcome={outcome}")
        self.run_id = run_id
        self.outcome = outcome


class LexiconRunFailedError(CueFlowError):
    """An internal terminology-discovery Run did not complete."""

    def __init__(self, run_id: str, outcome: str) -> None:
        super().__init__(f"Suggested Terms extraction {run_id} finished with outcome={outcome}")
        self.run_id = run_id
        self.outcome = outcome


class SuppressionConflictError(ContractError):
    """An explicit lexicon write needs a user choice about Trash/Blacklist state."""

    def __init__(self, normalized_surface_form: str, conflicts: tuple[str, ...]) -> None:
        super().__init__(
            "term is currently suppressed; choose remove_and_add, keep_and_add, or cancel"
        )
        self.normalized_surface_form = normalized_surface_form
        self.conflicts = conflicts


class ExportBlockedError(CueFlowError):
    """The export gate rejected the current project state."""
