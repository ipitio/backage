"""Compatibility exports for concurrent owner updates."""

from .owners.batch import (
    OwnerBatchEffects,
    OwnerBatchExecution,
    OwnerBatchItem,
    OwnerBatchRequest,
    OwnerBatchService,
    OwnerRetirementRepository,
    QueuedOwner,
    allocate_owner_worker_counts,
    parse_owner_queue,
)

__all__ = [
    "OwnerBatchEffects",
    "OwnerBatchExecution",
    "OwnerBatchItem",
    "OwnerBatchRequest",
    "OwnerBatchService",
    "OwnerRetirementRepository",
    "QueuedOwner",
    "allocate_owner_worker_counts",
    "parse_owner_queue",
]
