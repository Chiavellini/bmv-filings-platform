"""Shared acquisition boundary for the root document platform.

Applications consume estate releases; operators and schedulers invoke
``QuarterlyAcquisitionService``. Source adapters remain implementation details.
"""

from src.acquisition.ledger import AcquisitionLedger
from src.acquisition.models import (
    AcquisitionSource,
    FetchedArtifact,
    IssuerSpec,
    ProjectMembership,
    SourceRecord,
)
from src.acquisition.registry import (
    IssuerRegistry,
    load_issuer_registry,
)
from src.acquisition.service import QuarterlyAcquisitionService
from src.acquisition.writer import EstateWriter, StoreResult


__all__ = [
    "AcquisitionLedger",
    "AcquisitionSource",
    "EstateWriter",
    "FetchedArtifact",
    "IssuerRegistry",
    "IssuerSpec",
    "ProjectMembership",
    "QuarterlyAcquisitionService",
    "SourceRecord",
    "StoreResult",
    "load_issuer_registry",
]
