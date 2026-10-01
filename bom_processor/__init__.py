"""Deterministic, prompt-independent BOM processing core."""

from .core import (
    AzureBlobStorage,
    BlobConflictError,
    BlobNotFoundError,
    BomProcessor,
    InMemoryStorage,
    ProcessingError,
    ProcessingRequest,
    SourceDocument,
)
from .master_registry import (
    ApprovalRecord, ApprovedVariant, CandidateRecord, LocalRegistryStore,
    FixtureBinding, MasterRegistry, RegistryError, StructuralProfile, VariantSignature,
    profile_bytes,
)
from .blob_registry import BlobRegistryReader, RegistrySelection
from .job_orchestrator import (
    AzureJobStarter, AzureSasFactory, JobOrchestrator, JobStarter, SasFactory,
    SasUrls, StartResult, StorageReader, WorkItem, redact_url,
)
from .remote import RemoteBomProcessor
from .candidates import CandidateRequest, CandidateRequiredError, AggregateCandidateRequiredError
from .candidates import unique_fixtures
from .candidate_authoring import (AuthoringError, AuthoringPrompt, AuthoringResult,
                                   CandidateAuthoringService, ModelAuthor, SourceAnalysis)
from .candidate_validation import (CandidateValidationError, CandidateValidationResult,
                                    CandidateValidationService)
from .admin_promotion import AdminDecisionService
from .candidate_status import CandidateStatusError, CandidateStatusService
from .candidate_automation import CandidateAutomationService, AzureCandidateJobStarter
from .candidate_review import CandidateReviewError, CandidateReviewService
from .semantic import (AssemblyIdentity, BomComparison, BomReconciliation, BomSemantic,
                       compare_boms, normalize_bom, reconcile_boms)
from .package_resolution import PackageResolutionError, ParentPackageService

__all__ = [
    "AzureBlobStorage",
    "BlobConflictError",
    "BlobNotFoundError",
    "BomProcessor",
    "InMemoryStorage",
    "ProcessingError",
    "ProcessingRequest",
    "SourceDocument",
    "ApprovalRecord",
    "ApprovedVariant",
    "CandidateRecord",
    "FixtureBinding",
    "LocalRegistryStore",
    "MasterRegistry",
    "RegistryError",
    "StructuralProfile",
    "VariantSignature",
    "profile_bytes",
    "BlobRegistryReader", "RegistrySelection",
    "AzureJobStarter", "AzureSasFactory", "JobOrchestrator", "JobStarter",
    "SasFactory", "SasUrls", "StartResult", "StorageReader", "WorkItem", "redact_url",
    "RemoteBomProcessor",
    "CandidateRequest", "CandidateRequiredError", "AggregateCandidateRequiredError",
    "unique_fixtures",
    "AuthoringError", "AuthoringPrompt", "AuthoringResult", "CandidateAuthoringService",
    "ModelAuthor", "SourceAnalysis",
    "CandidateValidationError", "CandidateValidationResult", "CandidateValidationService",
    "AdminDecisionService",
    "CandidateStatusError", "CandidateStatusService",
    "CandidateAutomationService", "AzureCandidateJobStarter",
    "CandidateReviewError", "CandidateReviewService",
    "AssemblyIdentity", "BomComparison", "BomReconciliation", "BomSemantic",
    "compare_boms", "normalize_bom", "reconcile_boms",
    "PackageResolutionError", "ParentPackageService",
]
