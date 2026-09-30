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
from .candidate_authoring import (AuthoringError, AuthoringPrompt, AuthoringResult,
                                   CandidateAuthoringService, ModelAuthor, SourceAnalysis)
from .candidate_validation import (CandidateValidationError, CandidateValidationResult,
                                    CandidateValidationService)
from .admin_promotion import AdminDecisionService
from .candidate_status import CandidateStatusError, CandidateStatusService
from .candidate_automation import CandidateAutomationService, AzureCandidateJobStarter
from .candidate_review import CandidateReviewError, CandidateReviewService
from .semantic import AssemblyIdentity, BomComparison, BomSemantic, compare_boms, normalize_bom

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
    "AuthoringError", "AuthoringPrompt", "AuthoringResult", "CandidateAuthoringService",
    "ModelAuthor", "SourceAnalysis",
    "CandidateValidationError", "CandidateValidationResult", "CandidateValidationService",
    "AdminDecisionService",
    "CandidateStatusError", "CandidateStatusService",
    "CandidateAutomationService", "AzureCandidateJobStarter",
    "CandidateReviewError", "CandidateReviewService",
    "AssemblyIdentity", "BomComparison", "BomSemantic", "compare_boms", "normalize_bom",
]
