"""agent_runtime — the execution runtime for declaratively-defined agents.

One lean async process hosting many agents as dormant config records, activated
into transient bounded tasks by agent_bus trigger events. See documents/ for the
design; this package is the executor.
"""

__version__ = "0.1.0"
