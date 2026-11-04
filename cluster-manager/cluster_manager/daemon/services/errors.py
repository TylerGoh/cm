"""Errors the services raise on purpose. The API turns them into 4xx responses."""


class AgentNotFound(Exception):
    """No agent with that IP."""


class ActionRefused(Exception):
    """The action doesn't apply to the agent or fleet as it is; the message says why."""
