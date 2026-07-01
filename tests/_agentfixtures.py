"""Shared discovery of agent fixtures: every ``<name>.graph.json`` paired with its
``<name>.golden.json`` under ``tests/fixtures/``.

Tests parametrize over these pairs, so they are FIXTURE-DRIVEN: adding a new agent means
dropping two files here — no test code changes, and every lowering test runs against it
automatically. No concrete agent value is ever embedded in a test body.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"
_GRAPH_SUFFIX = ".graph.json"
_GOLDEN_SUFFIX = ".golden.json"


def agent_fixture_params() -> list:
    """One pytest.param(graph, golden) per discovered (graph, golden) fixture pair."""
    params = []
    for graph_path in sorted(FIXTURES.glob("*" + _GRAPH_SUFFIX)):
        name = graph_path.name[: -len(_GRAPH_SUFFIX)]
        golden_path = graph_path.with_name(name + _GOLDEN_SUFFIX)
        if golden_path.exists():
            params.append(
                pytest.param(
                    json.loads(graph_path.read_text()),
                    json.loads(golden_path.read_text()),
                    id=name,
                )
            )
    if not params:  # a fixture dir with no pairs is a mistake, not an empty pass
        raise RuntimeError(f"no <name>{_GRAPH_SUFFIX}/{_GOLDEN_SUFFIX} pairs under {FIXTURES}")
    return params
