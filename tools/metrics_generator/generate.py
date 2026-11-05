"""Generates the worker contract block for agent/agent/worker_client.py from a YAML file.

    python generate.py worker.yaml

Writes generated_worker_contract.py next to this script. Paste its whole contents over the block
between the BEGIN and END worker contract markers in worker_client.py. See README.md."""

import json
import keyword
import re
import sys
from enum import StrEnum, auto
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "generated_worker_contract.py"
SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")
CONSTANT_CASE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# Keys every metrics document in Elastic already carries (agent/agent/metrics_exporter.py,
# DOCUMENT_KEYS), so no metric field may take one of them.
DOCUMENT_KEYS = frozenset({"at", "running_version", "updating", "host", "ipv4", "location"})


# --- the YAML file's shape ---

def checked_command(value: str) -> str:
    """A command is appended to WORKER_URL with nothing in between (the base URL ends in something
    like `?command=`). It may contain spaces (`show bind`), which httpx sends as %20, so writing
    `show bind` or `show%20bind` gives the same URL. Not allowed: empty, a leading slash, spaces at
    either end (almost always a typo), and control characters such as a newline or a tab."""
    if not value:
        raise ValueError("a command can't be empty")
    if value.startswith("/"):
        raise ValueError(f"{value!r} must not start with /: commands are appended to WORKER_URL as-is")
    if value != value.strip():
        raise ValueError(f"{value!r} starts or ends with a space")
    if any(not character.isprintable() for character in value):
        raise ValueError(f"{value!r} contains a control character (a newline or a tab, say)")
    return value


class MetricKind(StrEnum):
    COUNTER = auto()        # cumulative since the worker started: the amount over a window is newest - oldest
    GAUGE = auto()          # a current value: the reading is the value


class MetricSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str                           # intake: its key in the endpoint's JSON, exactly as the worker sends it
    field: str                          # its name in Elastic, and the WorkerMetrics field; snake_case
    type: MetricKind
    description: str | None = None      # becomes a comment after the field

    @field_validator("field")
    @classmethod
    def _field_is_a_python_name(cls, value: str) -> str:
        if not SNAKE_CASE.match(value) or keyword.iskeyword(value):
            raise ValueError(f"{value!r} must be snake_case and not a Python keyword")
        if value in DOCUMENT_KEYS:
            raise ValueError(f"{value!r} is already a key of every metrics document; "
                             f"choose another (taken: {', '.join(sorted(DOCUMENT_KEYS))})")
        return value


class EndpointSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    command: str                        # appended as-is to the worker's base URL; answers one flat JSON object
    constant: str | None = None         # the Python name for it; derived from the command when left out
    metrics: list[MetricSpec] = Field(min_length=1)

    @field_validator("command")
    @classmethod
    def _command_is_appended(cls, value: str) -> str:
        return checked_command(value)

    @field_validator("constant")
    @classmethod
    def _constant_is_constant_case(cls, value: str | None) -> str | None:
        if value is not None and not CONSTANT_CASE.match(value):
            raise ValueError(f"{value!r} must be CONSTANT_CASE")
        return value


class ContractSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version_command: str                # answers {"version": "..."}; also the health ping
    reload_phase1_command: str          # GET, no body, blocks until phase 1 is done
    reload_phase2_command: str          # GET, no body, blocks until the worker runs the new code
    endpoints: list[EndpointSpec] = Field(min_length=1)

    @field_validator("version_command", "reload_phase1_command", "reload_phase2_command")
    @classmethod
    def _commands_are_appended(cls, value: str) -> str:
        return checked_command(value)


# --- checks across the whole file ---

def endpoint_constant(endpoint: EndpointSpec) -> str:
    """The endpoint's Python name: given, or derived from the command (stats.queue -> STATS_QUEUE_ENDPOINT)."""
    if endpoint.constant is not None:
        return endpoint.constant
    stem = re.sub(r"[^A-Za-z0-9]+", "_", endpoint.command).strip("_").upper()
    if not stem or stem[0].isdigit():
        raise SystemExit(f"generate: can't name {endpoint.command!r} from its command; give it a `constant`")
    return f"{stem}_ENDPOINT"


def check(contract: ContractSpec) -> None:
    """What the model can't check one item at a time. worker_client._derive would reject the
    same things at import, but here the error names the YAML entry."""
    constants: dict[str, str] = {}
    fields: dict[str, str] = {}
    names: dict[str, str] = {}
    for endpoint in contract.endpoints:
        constant = endpoint_constant(endpoint)
        if constant in constants:
            raise SystemExit(f"generate: {endpoint.command} and {constants[constant]} both become {constant}")
        constants[constant] = endpoint.command
        for metric in endpoint.metrics:
            if metric.field in fields:
                raise SystemExit(f"generate: field {metric.field} is used by {fields[metric.field]} "
                                 f"and {endpoint.command} {metric.name}")
            fields[metric.field] = f"{endpoint.command} {metric.name}"
            if metric.name in names:
                raise SystemExit(f"generate: name {metric.name!r} appears on {names[metric.name]} "
                                 f"and {endpoint.command}; each name may appear once")
            names[metric.name] = endpoint.command


# --- rendering ---

def quoted(value: str) -> str:
    """A double-quoted Python string literal, like the rest of the code."""
    return json.dumps(value)


def render(contract: ContractSpec, source: str) -> str:
    width = max(len(m.field) + 1 for e in contract.endpoints for m in e.metrics)   # "field:" padded
    lines = [
        f"# --- BEGIN worker contract: generated by tools/metrics_generator from {source}; "
        "paste over this whole block ---",
        "",
        "# Commands on the worker, each appended as-is to the base URL (WORKER_URL). Every query and",
        "# command is a GET with no body.",
        f"VERSION_COMMAND = {quoted(contract.version_command)}           # version() and health() both use it",
        f"RELOAD_PHASE1_COMMAND = {quoted(contract.reload_phase1_command)}    # blocks until done; success means done",
        f"RELOAD_PHASE2_COMMAND = {quoted(contract.reload_phase2_command)}    # blocks until the worker runs the new code",
        "",
        "# Metric commands: each answers one flat JSON object.",
    ]
    for endpoint in contract.endpoints:
        lines.append(f"{endpoint_constant(endpoint)} = MetricSource({quoted(endpoint.command)})")
    lines += [
        "",
        "",
        "class WorkerMetrics(BaseModel):",
        '    """One flat reading of the worker at one instant. Every field declares a MetricType,',
        '    a MetricSource and the name it has in that command\'s answer (validation_alias)."""',
        "    model_config = ConfigDict(frozen=True)",
    ]
    for endpoint in contract.endpoints:
        lines += ["", f"    # {endpoint.command}"]
        for metric in endpoint.metrics:
            kind = "MetricType.COUNTER" if metric.type is MetricKind.COUNTER else "MetricType.GAUGE"
            line = (f"    {metric.field + ':':<{width}} Annotated[float, {kind}, {endpoint_constant(endpoint)}, "
                    f"Field(validation_alias={quoted(metric.name)})]")
            if metric.description:
                line += f"    # {metric.description}"
            lines.append(line)
    lines += ["", "# --- END worker contract ---", ""]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__, file=sys.stderr)
        return 2
    source = Path(argv[1])
    try:
        raw = yaml.safe_load(source.read_text())
        contract = ContractSpec.model_validate(raw)
    except FileNotFoundError:
        print(f"generate: no such file: {source}", file=sys.stderr)
        return 2
    except (yaml.YAMLError, ValidationError) as error:
        print(f"generate: {source} is not a valid contract:\n{error}", file=sys.stderr)
        return 2
    check(contract)

    OUTPUT.write_text(render(contract, source.name))
    counters = sum(m.type is MetricKind.COUNTER for e in contract.endpoints for m in e.metrics)
    gauges = sum(m.type is MetricKind.GAUGE for e in contract.endpoints for m in e.metrics)
    print(f"wrote {OUTPUT}")
    print(f"{len(contract.endpoints)} metric commands, {counters} counters, {gauges} gauges")
    print("paste it over the worker contract block in agent/agent/worker_client.py, then update")
    print("agent/agent/performance.py, which reads the WorkerMetrics fields by name")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
