"""
Multi-Agent Engineering Control Plane.

Provides task specification, agent capability registry, deterministic routing,
specialized independent reviewers, review reconciliation, verification plans,
evidence ledgers, transparent performance metrics, project adapters, human authority boundaries,
and closed-loop governed task orchestration.
"""

from src.control_plane.task_spec import (
    TaskSpec,
    InvalidStateTransitionError,
    TaskSpecValidationError,
    VALID_TASK_STATES,
)
from src.control_plane.agent_registry import (
    AgentProfile,
    AgentRegistry,
    BUILTIN_AGENTS,
)
from src.control_plane.agent_execution import (
    AgentBackend,
    AgentBackendRegistry,
    AgentExecutionResult,
    AgentExecutionError,
    AgentUnavailableError,
    FakeAgentBackend,
    SubprocessAgentBackend,
)
from src.control_plane.git_baseline import (
    GitBaseline,
    RepositoryDelta,
    capture_baseline,
    capture_delta,
)
from src.control_plane.router import (
    TaskRouter,
    RoutingDecision,
)
from src.control_plane.reviewers import (
    ReviewerRole,
    REVIEWER_ROLES,
    get_reviewer_role,
    list_reviewer_roles,
)
from src.control_plane.review_runner import (
    SingleReviewResult,
    ReviewCycleResult,
    ReviewRunner,
    parse_and_validate_findings,
)
from src.control_plane.reconciliation import (
    ReviewFinding,
    ReconciliationResult,
    ReviewReconciler,
    ReconciliationValidationError,
)
from src.control_plane.verification import (
    VerificationStep,
    VerificationPlan,
    VerificationError,
)
from src.control_plane.evidence_ledger import (
    EvidenceEntry,
    EvidenceLedger,
    redact_sensitive_data,
)
from src.control_plane.metrics import (
    AgentMetricSummary,
    PerformanceMetricsSummary,
    MetricsCalculator,
)
from src.control_plane.project_adapter import (
    ProjectContext,
    ProjectAdapter,
)
from src.control_plane.human_boundary import (
    HumanBoundaryGate,
    HumanDecisionPacket,
    BoundaryCheckResult,
    HUMAN_BOUNDARY_TRIGGERS,
)
from src.control_plane.orchestrator import (
    GovernedTaskOrchestrator,
    OrchestrationConfig,
    OrchestrationResult,
)
from src.control_plane.role_binding import (
    RoleDescriptor,
    RoleBinding,
    RoleBindingRegistry,
    RoleExecutionRequest,
    RoleExecutionResult,
    RoleDispatcher,
    IndependenceStatus,
    RoleNotConfiguredError,
    RoleExecutionError,
    get_default_role_registry,
)

__all__ = [
    "TaskSpec",
    "InvalidStateTransitionError",
    "TaskSpecValidationError",
    "VALID_TASK_STATES",
    "AgentProfile",
    "AgentRegistry",
    "BUILTIN_AGENTS",
    "AgentBackend",
    "AgentBackendRegistry",
    "AgentExecutionResult",
    "AgentExecutionError",
    "AgentUnavailableError",
    "FakeAgentBackend",
    "SubprocessAgentBackend",
    "GitBaseline",
    "RepositoryDelta",
    "capture_baseline",
    "capture_delta",
    "TaskRouter",
    "RoutingDecision",
    "ReviewerRole",
    "REVIEWER_ROLES",
    "get_reviewer_role",
    "list_reviewer_roles",
    "SingleReviewResult",
    "ReviewCycleResult",
    "ReviewRunner",
    "parse_and_validate_findings",
    "ReviewFinding",
    "ReconciliationResult",
    "ReviewReconciler",
    "ReconciliationValidationError",
    "VerificationStep",
    "VerificationPlan",
    "VerificationError",
    "EvidenceEntry",
    "EvidenceLedger",
    "redact_sensitive_data",
    "AgentMetricSummary",
    "PerformanceMetricsSummary",
    "MetricsCalculator",
    "ProjectContext",
    "ProjectAdapter",
    "HumanBoundaryGate",
    "HumanDecisionPacket",
    "BoundaryCheckResult",
    "HUMAN_BOUNDARY_TRIGGERS",
    "GovernedTaskOrchestrator",
    "OrchestrationConfig",
    "OrchestrationResult",
    "RoleDescriptor",
    "RoleBinding",
    "RoleBindingRegistry",
    "RoleExecutionRequest",
    "RoleExecutionResult",
    "RoleDispatcher",
    "IndependenceStatus",
    "RoleNotConfiguredError",
    "RoleExecutionError",
    "get_default_role_registry",
]
