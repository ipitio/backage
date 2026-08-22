"""Public owner identity and authenticated discovery surface."""

from .authenticated import (
    DiscoveryError,
    OwnerIdentity,
    OwnerIdentityCache,
    OwnerIdentityResolver,
    OwnerLookupResult,
)

__all__ = [
    "DiscoveryError",
    "OwnerIdentity",
    "OwnerIdentityCache",
    "OwnerIdentityResolver",
    "OwnerLookupResult",
]
