"""Streamlit client workspace.

Public entry point: ``ai_powerbi_analyst.ui.app:render_app``. The executable
script path for Streamlit is ``src/ai_powerbi_analyst/ui/app.py``.
"""

from .app import render_app
from .models import AppWorkspace, WorkspaceConfig

__all__ = ["AppWorkspace", "WorkspaceConfig", "render_app"]
