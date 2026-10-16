"""Governed Coding Agent (gca).

Pipeline: WorkItem -> approval -> isolated workspace -> agent loop (model picks actions
via tools, harness enforces policy) -> independent verifier -> report + audit trail.
"""

__version__ = "0.1.0"
