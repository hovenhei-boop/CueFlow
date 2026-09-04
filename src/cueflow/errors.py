class CueFlowError(Exception):
    """Base error for explicit CueFlow failures."""


class ContractError(CueFlowError):
    """A frozen schema or architecture invariant was violated."""


class IntegrityError(CueFlowError):
    """Persisted control-plane or artifact data is inconsistent."""


class SourceMissingError(IntegrityError):
    """A registered external source path is unavailable."""


class UnsupportedReferenceError(ContractError):
    """A Reference input is outside the explicit v0.5.2 contract."""


class ProviderError(CueFlowError):
    """A provider returned an explicit failure."""


class ProviderUnavailableError(ProviderError):
    """A provider runtime or credential is unavailable."""


class DeliveryAmbiguousError(ProviderError):
    """A remote request may have been delivered without a definite response."""


class ExportBlockedError(CueFlowError):
    """The export gate rejected the current project state."""
