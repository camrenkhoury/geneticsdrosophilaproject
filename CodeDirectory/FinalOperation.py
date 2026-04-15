"""
Compatibility re-export for historical imports.

The real automated orchestration lives in:
    pi_app/legacy_pi/FinalOperation.py

Older modules in this repo still import ``CodeDirectory.FinalOperation``.
To avoid splitting the automation logic across two files, this module only
re-exports the real implementation. Edit the legacy_pi file when changing the
sorting workflow, channel detection sequencing, chamber handling, or assay
handoff behavior.
"""

from pi_app.legacy_pi.FinalOperation import *  # noqa: F401,F403
