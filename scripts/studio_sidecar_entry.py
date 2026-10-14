"""Absolute-import entrypoint used by PyInstaller."""

import runpy
import sys

# In the packaged app ``sys.executable`` is this program, not a Python, so a child process
# started as ``<exe> -m <module> ...`` (how Studio decodes a PBIX in an isolated, memory-bounded
# process) would start a second sidecar. Run those modules here instead. Only the listed
# read-only extractors are allowed; anything else falls through to the normal startup.
_CHILD_MODULES = frozenset({"ai_powerbi_analyst.studio.pbix_extract"})


def _run_child_module() -> bool:
    if len(sys.argv) >= 3 and sys.argv[1] == "-m" and sys.argv[2] in _CHILD_MODULES:
        module = sys.argv[2]
        sys.argv = [module, *sys.argv[3:]]
        runpy.run_module(module, run_name="__main__", alter_sys=True)
        return True
    return False


if __name__ == "__main__":
    if not _run_child_module():
        from ai_powerbi_analyst.studio.__main__ import main

        main()
