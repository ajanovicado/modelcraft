"""Safe, deterministic Power BI report composition.

The package deliberately keeps language-model output on the typed contract side
of the boundary. Only fixed recipes can reach PBIR tooling or the filesystem.
"""

from ai_powerbi_analyst.powerbi.backend import BackendReceipt, PbiCliBackend
from ai_powerbi_analyst.powerbi.cli import (
    AuthoringCliAdapter,
    PbiCliAdapter,
    PowerBICapabilities,
    discover_capabilities,
)
from ai_powerbi_analyst.powerbi.composer import (
    BaselineReportInvalid,
    BaseReportChanged,
    CatalogVersionMismatch,
    ComposerProgress,
    ComposerResult,
    SafePowerBIComposer,
)
from ai_powerbi_analyst.powerbi.kill_test import (
    DesktopConfirmation,
    Phase0KillTestReport,
    Phase0KillTestRunner,
)
from ai_powerbi_analyst.powerbi.mock_backend import MockPowerBIBackend
from ai_powerbi_analyst.powerbi.recipes import DashboardBlueprint, RecipeCompiler
from ai_powerbi_analyst.powerbi.transaction import TransactionReceipt, UndoReceipt
from ai_powerbi_analyst.powerbi.validation import make_external_validator

__all__ = [
    "AuthoringCliAdapter",
    "BackendReceipt",
    "BaselineReportInvalid",
    "BaseReportChanged",
    "CatalogVersionMismatch",
    "ComposerProgress",
    "ComposerResult",
    "DashboardBlueprint",
    "DesktopConfirmation",
    "MockPowerBIBackend",
    "PbiCliAdapter",
    "PbiCliBackend",
    "Phase0KillTestReport",
    "Phase0KillTestRunner",
    "PowerBICapabilities",
    "RecipeCompiler",
    "SafePowerBIComposer",
    "TransactionReceipt",
    "UndoReceipt",
    "discover_capabilities",
    "make_external_validator",
]
