"""Prefect DAG: wire all five pipeline tasks into a single ingestion flow.

Stage order
-----------
1. DiscoveryTask      — catalog documents from configured data-source connectors
2. DetectionTask      — run three-layer PII detection on each document's blocks
3. ExtractionTask     — persist results under the active storage policy
4. QATask             — validate completeness and flag anomalies
5. ErrorHandlerTask   — handle failures at each stage with retry / escalation

Each document is processed independently. After every page, a checkpoint
is written to PostgreSQL so crashed jobs resume from last_completed_page + 1.
"""
from __future__ import annotations

from app.tasks.discovery import DataSourceConnector


def build_pipeline():
    """Construct and return the Prefect flow for the full ingestion pipeline."""
    ...


def run_pipeline(
    source_paths: list[str],
    mode: str = "strict",
    connectors: list[DataSourceConnector] | None = None,
) -> None:
    """Execute the pipeline for the given source paths in the specified mode."""
    ...
