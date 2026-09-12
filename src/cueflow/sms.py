from __future__ import annotations

from enum import Enum
from typing import Protocol


class SmsPurpose(str, Enum):
    PHONE_CONTINUE = "phone_continue"
    PASSWORD_RESET = "password_reset"
    PHONE_CHANGE = "phone_change"
    ACCOUNT_ERASURE = "account_erasure"
    PHONE_APPEAL = "phone_appeal"


class SmsProvider(Protocol):
    def send_code(self, *, phone: str, code: str, purpose: SmsPurpose) -> None: ...
