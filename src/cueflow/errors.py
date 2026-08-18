class CueFlowError(Exception):
    """Base error for explicit CueFlow failures."""


class ContractError(CueFlowError):
    """A frozen schema or architecture invariant was violated."""


class IntegrityError(CueFlowError):
    """Persisted control-plane or artifact data is inconsistent."""


class ProviderError(CueFlowError):
    """Base provider failure."""


class ProviderUnavailableError(ProviderError):
    """A required provider runtime or credential is unavailable."""


class DeliveryAmbiguousError(ProviderError):
    """A remote request may have been delivered but no definite result was received."""


class ExportBlockedError(CueFlowError):
    """The export gate rejected the current project state."""
