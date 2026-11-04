from typing import get_type_hints

from agent.models import AgentMemory, AgentMemoryChanges


def test_memory_changes_mirror_agent_memory() -> None:
    """Every AgentMemory field is a key of AgentMemoryChanges, with the same type, and nothing else."""
    memory_fields = {name: field.annotation for name, field in AgentMemory.model_fields.items()}
    change_keys = get_type_hints(AgentMemoryChanges)
    assert change_keys == memory_fields


def test_memory_changes_keys_are_optional() -> None:
    """A missing key means "leave this field unchanged", so no key may be required."""
    assert AgentMemoryChanges.__required_keys__ == frozenset()
