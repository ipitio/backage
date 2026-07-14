"""Compatibility exports for owner discovery page admission."""

from .owners.pages import (
    OwnerPageAdmissionConfig,
    OwnerPageAdmissionResult,
    admit_owner_page,
)

__all__ = [
    "OwnerPageAdmissionConfig",
    "OwnerPageAdmissionResult",
    "admit_owner_page",
]
