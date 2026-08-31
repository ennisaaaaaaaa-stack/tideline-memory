"""Shim for Hermes' agent.memory_provider.MemoryProvider base class.

tideline_provider.py was written for Hermes Agent's plugin layer and imports
this base class. Kimi Code CLI has no such framework — hooks are standalone
processes — so we provide a minimal stub to let the provider module import
and run unchanged.
"""


class MemoryProvider:
    """Minimal stand-in for the Hermes MemoryProvider base class."""

    @property
    def name(self) -> str:
        return "memory"

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        pass

    def shutdown(self) -> None:
        pass
