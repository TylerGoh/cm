#!/usr/bin/env python3
"""Prepares this folder from config.yaml: renames the placeholder terms and writes the generated
blocks (the worker contract, the inventory fields and their agent settings).

    cp /path/to/your/config.yaml .      # next to this script
    python3 prepare.py                  # needs pyyaml and pydantic (pip install pyyaml pydantic)
    python3 prepare.py --check          # validates config.yaml, changes nothing

In a patch it runs only from the patch's apply.sh, as  prepare.py --install DIR : it reads
DIR/config.yaml and renames as DIR was prepared. A patch never has a config.yaml of its own, so
it can't be prepared with a stale or wrong copy.

It works on both kinds of folder it ships in:
  - the initial install (toolkit_cm/: agent/, cluster-manager/, file-server/, ...)
  - a patch (patch_cm-vN/: a PATCH file, and <folder>/old/ and <folder>/new/ copies)

Running it again with the same config.yaml is safe: the renames are already done and the blocks
are written again. New `rename:` keys are added to a folder already prepared; changing one it
was prepared with is refused (unzip again).
config.example.yaml documents every key. Keys this version doesn't know are skipped with a warning,
so a config.yaml written for a newer release still works here.
"""
import ast
import hashlib
import json
import keyword
import os
import re
import string
import sys
from enum import StrEnum, auto
from pathlib import Path
from typing import Any
from urllib.parse import unquote

try:
    import yaml
    from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
except ImportError as missing:
    sys.exit(f"prepare: {missing.name} is missing; install it with: pip install pyyaml pydantic")

HERE = Path(__file__).resolve().parent
CONFIG_NAME = "config.yaml"
STAMP = "PREPARED"

# Placeholders this version knows. A rename: key outside this list is skipped with a warning.
# reload_phase renames both phases; reload_phase1 and reload_phase2 rename one each. Longer
# placeholders are renamed first, so the two kinds can be given together.
PLACEHOLDERS = ("hypertree", "program_log", "reload_phase", "reload_phase1", "reload_phase2", "etc_dir")
SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")
CONSTANT_CASE = re.compile(r"^[A-Z][A-Z0-9_]*$")
ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Keys every Elastic document already carries (agent/agent/metrics_exporter.py DOCUMENT_KEYS, and
# what agent/agent/inventory.py puts in each inventory record); neither a metric field nor an
# inventory field may take one.
METRICS_DOCUMENT_KEYS = frozenset({"@timestamp", "at", "running_version", "updating", "hostname", "ip", "cluster_name"})
INVENTORY_RECORD_KEYS = frozenset({"at", "kind", "intf", "hostname", "ip", "cluster_name"})

# Never renamed: this script, the configs, the patch's own files. Folders never walked.
SKIP_FILES = frozenset({"prepare.py", CONFIG_NAME, "config.example.yaml", STAMP, "PATCH", "apply.sh"})
SKIP_DIRS = frozenset({".git", ".venv", "venv", "__pycache__", ".pytest_cache", "node_modules",
                       "build", "dist", ".patches"})


def warn(message: str) -> None:
    print(f"prepare: warning: {message}", file=sys.stderr)


# --- config.yaml ---

class Lenient(BaseModel):
    """Unknown keys are skipped (warned about by unknown_keys), so newer configs still load."""
    model_config = ConfigDict(extra="ignore", frozen=True)


def checked_command(value: str) -> str:
    """Appended to WORKER_URL with nothing in between. May contain spaces (sent as %20) and
    {config.<setting>} fields; not empty, no leading slash, no spaces at either end, no control
    characters. The fields are checked against the agent's settings later."""
    if not value:
        raise ValueError("a command can't be empty")
    if value.startswith("/"):
        raise ValueError(f"{value!r} must not start with /: commands are appended to WORKER_URL as-is")
    if value != value.strip():
        raise ValueError(f"{value!r} starts or ends with a space")
    if any(not character.isprintable() for character in value):
        raise ValueError(f"{value!r} contains a control character (a newline or a tab, say)")
    try:
        list(string.Formatter().parse(value))
    except ValueError as error:
        raise ValueError(f"{value!r}: {error}; write a literal brace as {{{{ or }}}}") from None
    return value


class MetricKind(StrEnum):
    COUNTER = auto()        # cumulative since the worker started: the amount over a window is newest - oldest
    GAUGE = auto()          # a current value: the reading is the value


class MetricSpec(Lenient):
    name: str                           # intake: its key in the endpoint's JSON, exactly as the worker sends it
    field: str                          # its name in Elastic, and the WorkerMetrics field; snake_case
    type: MetricKind
    description: str | None = None      # becomes a comment after the field

    @field_validator("field")
    @classmethod
    def _field_is_a_python_name(cls, value: str) -> str:
        if not SNAKE_CASE.match(value) or keyword.iskeyword(value):
            raise ValueError(f"{value!r} must be snake_case and not a Python keyword")
        if value in METRICS_DOCUMENT_KEYS:
            raise ValueError(f"{value!r} is already a key of every metrics document; "
                             f"choose another (taken: {', '.join(sorted(METRICS_DOCUMENT_KEYS))})")
        return value


class EndpointSpec(Lenient):
    command: str
    constant: str | None = None         # the Python name; derived from the command when left out
    metrics: list[MetricSpec] = Field(min_length=1)

    @field_validator("command")
    @classmethod
    def _command(cls, value: str) -> str:
        return checked_command(value)

    @field_validator("constant")
    @classmethod
    def _constant_is_constant_case(cls, value: str | None) -> str | None:
        if value is not None and not CONSTANT_CASE.match(value):
            raise ValueError(f"{value!r} must be CONSTANT_CASE")
        return value


class WorkerSpec(Lenient):
    version_command: str                # answers [["type","version","active"], ...]; also the health ping
    reload_phase1_command: str          # GET, no body, blocks until phase 1 is done
    reload_phase2_command: str          # GET, no body, blocks until the worker runs the new code
    endpoints: list[EndpointSpec] = Field(min_length=1)

    @field_validator("version_command", "reload_phase1_command", "reload_phase2_command")
    @classmethod
    def _commands(cls, value: str) -> str:
        return checked_command(value)


class SettingType(StrEnum):
    STR = "str"
    INT = "int"
    FLOAT = "float"
    BOOL = "bool"


class InventoryFieldSpec(Lenient):
    field: str                          # its name in the host index record
    env: str                            # an agent .env key; a new one is added to config.py
    type: SettingType = SettingType.STR # for a new env key
    description: str | None = None      # for config.yaml's reader only; not written anywhere

    @field_validator("field")
    @classmethod
    def _field(cls, value: str) -> str:
        if not SNAKE_CASE.match(value):
            raise ValueError(f"{value!r} must be snake_case")
        if value in INVENTORY_RECORD_KEYS:
            raise ValueError(f"{value!r} is already in every host record "
                             f"(taken: {', '.join(sorted(INVENTORY_RECORD_KEYS))})")
        return value

    @field_validator("env")
    @classmethod
    def _env(cls, value: str) -> str:
        if not ENV_KEY.match(value):
            raise ValueError(f"{value!r} must be an UPPER_CASE .env key")
        return value


class InventorySpec(Lenient):
    fields: list[InventoryFieldSpec] = []


# env: section -> the example each .env is made from, relative to the install's top folder.
# The .env is written next to its example (in a patch: at the top of that service's folder).
ENV_FILES = {
    "agent": "agent/.env.example",
    "cluster-manager": "cluster-manager/.env.example",
    "file-server": "file-server/.env.example",
    "postgres": "cluster-manager/deploy/postgres.env.example",
}


class Config(Lenient):
    rename: dict[str, str] = {}
    worker: WorkerSpec
    inventory: InventorySpec = InventorySpec()
    env: dict[str, dict[str, Any]] = {}


def unknown_keys(model: type[BaseModel], data: Any, where: str) -> list[str]:
    """Every key in data that model (or a nested model) doesn't declare, as a dotted path."""
    found: list[str] = []
    if not isinstance(data, dict):
        return found
    for key, value in data.items():
        if key not in model.model_fields:
            found.append(f"{where}{key}")
            continue
        annotation = model.model_fields[key].annotation
        inner = getattr(annotation, "__args__", (annotation,))
        for candidate in inner:
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                if isinstance(value, list):
                    for i, item in enumerate(value):
                        found += unknown_keys(candidate, item, f"{where}{key}[{i}].")
                else:
                    found += unknown_keys(candidate, value, f"{where}{key}.")
    return found


def skip_source_fields(raw: Any, name: str) -> None:
    """Drops inventory fields with a source: (desired, observed, busy, ...), which older configs
    have: the agent now always sends the desired and observed states, and those cover them all."""
    inventory = raw.get("inventory") if isinstance(raw, dict) else None
    fields = inventory.get("fields") if isinstance(inventory, dict) else None
    if not isinstance(fields, list):
        return
    for f in fields:
        if isinstance(f, dict) and "source" in f:
            warn(f"{name}: inventory field {f.get('field')} (source: {f['source']}) is no longer needed; "
                 f"desired_* and observed_* are always sent; skipped")
    inventory["fields"] = [f for f in fields if not (isinstance(f, dict) and "source" in f)]


def load_config(path: Path) -> tuple[Config, dict[str, str], str]:
    """The config, the renames to apply (placeholder -> name), and the file's sha256."""
    if not path.exists():
        raise SystemExit(f"prepare: no {path.name} in {path.parent}")
    raw_bytes = path.read_bytes()
    try:
        raw = yaml.safe_load(raw_bytes)
        skip_source_fields(raw, path.name)
        config = Config.model_validate(raw)
    except (yaml.YAMLError, ValidationError) as error:
        raise SystemExit(f"prepare: {path.name} is not valid:\n{error}") from None
    for key in unknown_keys(Config, raw, ""):
        warn(f"{path.name}: {key} is not known to this version; skipped")

    renames: dict[str, str] = {}
    for old, new in config.rename.items():
        if old not in PLACEHOLDERS:
            warn(f"{path.name}: rename.{old} is not a placeholder in this version; skipped")
            continue
        if not isinstance(new, str) or not SNAKE_CASE.match(new):
            raise SystemExit(f"prepare: rename.{old}: {new!r} must be snake_case")
        if new == old:
            continue
        clash = [p for p in PLACEHOLDERS if p in new]
        if clash:
            raise SystemExit(f"prepare: rename.{old}: {new!r} contains the placeholder {clash[0]!r}")
        renames[old] = new
    renames = dict(sorted(renames.items(), key=lambda item: len(item[0]), reverse=True))
    return config, renames, hashlib.sha256(raw_bytes).hexdigest()


# --- renaming: every form of each placeholder, like the old rename_term.sh ---

def forms(name: str) -> list[str]:
    words = name.split("_")
    return ["_".join(words), "_".join(words).upper(), "".join(w.capitalize() for w in words),
            " ".join(words), " ".join(words).capitalize()]


class Renamer:
    def __init__(self, renames: dict[str, str]) -> None:
        self._passes: list[tuple[re.Pattern[str], dict[str, str]]] = []
        for old, new in renames.items():
            mapping: dict[str, str] = {}
            for o, n in zip(forms(old), forms(new), strict=True):
                mapping.setdefault(o, n)
            pattern = re.compile("|".join(re.escape(k) for k in sorted(mapping, key=len, reverse=True)))
            self._passes.append((pattern, mapping))

    def __call__(self, text: str) -> str:
        for pattern, mapping in self._passes:
            text = pattern.sub(lambda m: mapping[m.group(0)], text)
        return text


def walk(root: Path) -> tuple[list[Path], list[Path]]:
    """Files to rename inside, and every file and folder under root (deepest first)."""
    files: list[Path] = []
    paths: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        base = Path(dirpath)
        for d in dirnames:
            paths.append(base / d)
        for f in filenames:
            if f in SKIP_FILES:
                continue
            files.append(base / f)
            paths.append(base / f)
    paths.sort(key=lambda p: len(p.parts), reverse=True)
    return files, paths


def read_text(path: Path) -> str | None:
    data = path.read_bytes()
    if b"\0" in data:
        return None
    try:
        return data.decode()
    except UnicodeDecodeError:
        return None


def rename_tree(root: Path, rename: Renamer) -> tuple[int, int]:
    files, paths = walk(root)
    edited = 0
    for path in files:
        text = read_text(path)
        if text is None:
            continue
        renamed = rename(text)
        if renamed != text:
            path.write_text(renamed)
            edited += 1
    moved = 0
    for path in paths:
        new_name = rename(path.name)
        if new_name != path.name:
            target = path.with_name(new_name)
            if target.exists():
                raise SystemExit(f"prepare: can't rename {path} to {target}: it exists")
            path.rename(target)
            moved += 1
    return edited, moved


# --- the agent's settings, read from its config.py ---

# "# BEGIN generated: <name>" to "# END generated: <name>". Files from v4 and earlier mark them
# "# --- BEGIN generated: <name> (...) ---"; both are read, so a patch still matches them.
BLOCK = re.compile(r"^(?P<indent>[ \t]*)# (?:--- )?BEGIN generated: (?P<name>[a-z ]+?)(?: \(.*\))?(?: ---)?[ \t]*\n"
                   r"(?P<body>.*?)"
                   r"^[ \t]*# (?:--- )?END generated: (?P=name)(?: ---)?[ \t]*$", re.S | re.M)


def without_blocks(text: str) -> str:
    return BLOCK.sub(lambda m: "", text)


def agent_settings(config_py: Path) -> set[str]:
    """AgentConfig's declared settings, leaving out the generated block (written again here)."""
    tree = ast.parse(without_blocks(config_py.read_text()))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "AgentConfig":
            return {n.target.id for n in node.body
                    if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)}
    raise SystemExit(f"prepare: no AgentConfig class in {config_py}")


# --- the generated blocks ---
# Text of ours (names like RELOAD_PHASE1_COMMAND) goes through the renamer; values from
# config.yaml (commands, metric names) are put in afterwards, exactly as written.

class Block:
    def __init__(self, rename: Renamer) -> None:
        self._rename = rename
        self._values: list[str] = []
        self.lines: list[str] = []

    def value(self, text: str) -> str:
        self._values.append(text)
        return f"\0{len(self._values) - 1}\0"

    def text(self) -> str:
        out = self._rename("\n".join(self.lines))
        return re.sub(r"\0(\d+)\0", lambda m: self._values[int(m.group(1))], out)


def quoted(value: str) -> str:
    return json.dumps(value)


def endpoint_constant(endpoint: EndpointSpec) -> str:
    if endpoint.constant is not None:
        return endpoint.constant
    # %20 and the like are decoded first, so show%20counters names like show counters
    readable = unquote(re.sub(r"\{[^}]*\}", "", endpoint.command))
    stem = re.sub(r"[^A-Za-z0-9]+", "_", readable).strip("_").upper()
    if not stem or stem[0].isdigit():
        raise SystemExit(f"prepare: can't name {endpoint.command!r} from its command; give it a `constant`")
    return f"{stem}_ENDPOINT"


def check_worker(worker: WorkerSpec, settings: set[str], rename: Renamer) -> dict[str, str]:
    """Cross-entry checks, and every {config.x} field. Returns each command with its fields'
    setting names renamed (so {config.hypertree_dir} also works after renaming)."""
    commands = [worker.version_command, worker.reload_phase1_command, worker.reload_phase2_command,
                *(e.command for e in worker.endpoints)]
    fixed: dict[str, str] = {}
    for command in commands:
        out = []
        for literal, field, spec, conversion in string.Formatter().parse(command):
            out.append(literal.replace("{", "{{").replace("}", "}}"))
            if field is None:
                continue
            match = re.fullmatch(r"config\.([a-z_][a-z0-9_]*)", field)
            if not match:
                raise SystemExit(f"prepare: command {command!r}: {{{field}}} must be {{config.<setting>}}")
            setting = rename(match.group(1))
            if setting not in settings:
                raise SystemExit(f"prepare: command {command!r}: the agent has no setting {match.group(1)!r} "
                                 f"(settings: {', '.join(sorted(settings))})")
            out.append("{config." + setting + (f"!{conversion}" if conversion else "")
                       + (f":{spec}" if spec else "") + "}")
        fixed[command] = "".join(out)

    constants: dict[str, str] = {}
    fields: dict[str, str] = {}
    names: dict[str, str] = {}
    for endpoint in worker.endpoints:
        constant = endpoint_constant(endpoint)
        if constant in constants:
            raise SystemExit(f"prepare: {endpoint.command} and {constants[constant]} both become {constant}")
        constants[constant] = endpoint.command
        for metric in endpoint.metrics:
            if metric.field in fields:
                raise SystemExit(f"prepare: field {metric.field} is used by {fields[metric.field]} "
                                 f"and {endpoint.command} {metric.name}")
            fields[metric.field] = f"{endpoint.command} {metric.name}"
            if metric.name in names:
                raise SystemExit(f"prepare: name {metric.name!r} appears on {names[metric.name]} "
                                 f"and {endpoint.command}; each name may appear once")
            names[metric.name] = endpoint.command
    return fixed


def worker_block(worker: WorkerSpec, commands: dict[str, str], rename: Renamer) -> str:
    b = Block(rename)
    width = max(len(m.field) + 1 for e in worker.endpoints for m in e.metrics)
    b.lines += [
        "",
        "# Commands on the worker, each appended as-is to the base URL (WORKER_URL). Every query and",
        "# command is a GET with no body. Each is a format string: {config.<setting>} becomes that agent",
        "# setting when the request is made.",
        f"VERSION_COMMAND = {b.value(quoted(commands[worker.version_command]))}           # version() and health() both use it",
        f"RELOAD_PHASE1_COMMAND = {b.value(quoted(commands[worker.reload_phase1_command]))}    # blocks until done; success means done",
        f"RELOAD_PHASE2_COMMAND = {b.value(quoted(commands[worker.reload_phase2_command]))}    # blocks until the worker runs the new code",
        "",
        "# Metric commands: each answers one JSON object, every metric wrapped as {\"value\": ...}.",
    ]
    for endpoint in worker.endpoints:
        b.lines.append(f"{b.value(endpoint_constant(endpoint))} = MetricSource({b.value(quoted(commands[endpoint.command]))})")
    b.lines += [
        "",
        "",
        "class WorkerMetrics(BaseModel):",
        '    """One flat reading of the worker at one instant, each value already taken out of its',
        '    {"value": ...}. Every field declares a MetricType, a MetricSource and the name it has in',
        '    that command\'s answer (validation_alias). Counters are numbers; a gauge is a number or a',
        '    string, kept as the worker sent it."""',
        "    model_config = ConfigDict(frozen=True)",
    ]
    for endpoint in worker.endpoints:
        b.lines += ["", f"    # {b.value(endpoint.command)}"]
        for metric in endpoint.metrics:
            # a counter is always a number; a gauge may be a string the worker formats, e.g. "5d 18h"
            kind, py_type = (("MetricType.COUNTER", "float") if metric.type is MetricKind.COUNTER
                             else ("MetricType.GAUGE", "float | str"))
            line = (f"    {b.value(f'{metric.field}:'.ljust(width))} Annotated[{py_type}, {kind}, "
                    f"{b.value(endpoint_constant(endpoint))}, Field(validation_alias={b.value(quoted(metric.name))})]")
            if metric.description:
                line += f"    # {b.value(metric.description)}"
            b.lines.append(line)
    b.lines.append("")
    return b.text()


def new_settings(inventory: InventorySpec, settings: set[str], rename: Renamer) -> list[InventoryFieldSpec]:
    """The env: fields that aren't an existing agent setting: config.py declares them."""
    seen: set[str] = set()
    out = []
    for f in inventory.fields:
        if rename(f.env.lower()) not in settings and f.env not in seen:
            seen.add(f.env)
            out.append(f)
    return out


def check_inventory(inventory: InventorySpec, settings: set[str], rename: Renamer) -> None:
    names = [f.field for f in inventory.fields]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        raise SystemExit(f"prepare: inventory field repeated: {', '.join(repeated)}")
    for f in inventory.fields:
        if rename(f.env.lower()) in settings and f.type is not SettingType.STR:
            warn(f"inventory {f.field}: {f.env} is an existing agent setting; its type there is kept")


def inventory_block(inventory: InventorySpec, settings: set[str], rename: Renamer) -> str:
    b = Block(rename)
    b.lines.append("INVENTORY_FIELDS: dict[str, object] = {")
    for f in inventory.fields:
        setting = rename(f.env.lower()) if rename(f.env.lower()) in settings else f.env.lower()
        b.lines.append(f"    {b.value(quoted(f.field))}: config.{b.value(setting)},")
    b.lines += ["}", ""]
    return b.text()


def by_type(fields: list[InventoryFieldSpec]) -> list[tuple[SettingType, list[InventoryFieldSpec]]]:
    """The new settings grouped by type, in SettingType order; each group keeps config.yaml's order."""
    return [(t, group) for t in SettingType if (group := [f for f in fields if f.type is t])]


def config_py_block(inventory: InventorySpec, settings: set[str], rename: Renamer, indent: str) -> str:
    groups = []
    for setting_type, fields in by_type(new_settings(inventory, settings, rename)):
        groups.append([f"{indent}# {setting_type.value}"]
                      + [f"{indent}{f.env.lower()}: {setting_type.value}" for f in fields])
    return "".join("\n".join(g) + "\n" for g in join_groups(groups))


def env_example_block(inventory: InventorySpec, settings: set[str], rename: Renamer) -> str:
    groups = []
    for setting_type, fields in by_type(new_settings(inventory, settings, rename)):
        groups.append([f"# {setting_type.value}"] + [f"{f.env}=" for f in fields])
    return "".join("\n".join(g) + "\n" for g in join_groups(groups))


def join_groups(groups: list[list[str]]) -> list[list[str]]:
    """A blank line between groups."""
    return [g if i == 0 else [""] + g for i, g in enumerate(groups)]


def write_blocks(path: Path, config: Config, commands: dict[str, str], settings: set[str],
                 rename: Renamer) -> list[str]:
    text = path.read_text()

    def body(m: re.Match[str]) -> str:
        name, indent = m.group("name"), m.group("indent")
        if name == "worker contract":
            new = worker_block(config.worker, commands, rename)
        elif name == "inventory fields":
            new = inventory_block(config.inventory, settings, rename)
        elif name == "inventory settings" and path.suffix == ".py":
            new = config_py_block(config.inventory, settings, rename, indent)
        elif name == "inventory settings":
            new = env_example_block(config.inventory, settings, rename)
        else:
            warn(f"{path}: unknown generated block {name!r}; left as it is")
            return m.group(0)
        written.append(name)
        whole = m.group(0)
        start = whole.index("\n") + 1
        end = whole.rindex("\n") + 1
        return whole[:start] + new + whole[end:]

    written: list[str] = []
    new_text = BLOCK.sub(body, text)
    if new_text != text:
        path.write_text(new_text)
    return written


# --- .env files, from each .env.example and config.yaml's env: values ---

ENV_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
PLAIN_VALUE = re.compile(r"^[A-Za-z0-9_./:@+,=-]*$")


def env_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int | float):
        return str(value)
    if isinstance(value, list | dict):
        text = json.dumps(value)
        if "'" in text:
            raise SystemExit(f"prepare: env value {text} contains a single quote")
        return f"'{text}'"
    text = str(value)
    if PLAIN_VALUE.match(text):
        return text
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def env_entries(example: str) -> list[tuple[str | None, list[str]]]:
    """The example as (key, lines) entries; comments and blanks have key None. A quoted value
    may run over several lines."""
    entries: list[tuple[str | None, list[str]]] = []
    lines = example.splitlines()
    i = 0
    while i < len(lines):
        m = ENV_LINE.match(lines[i])
        if not m:
            entries.append((None, [lines[i]]))
            i += 1
            continue
        block = [lines[i]]
        value = m.group(2)
        quote = value[:1] if value[:1] in ("'", '"') else ""
        if quote and (len(value) == 1 or not value.rstrip().endswith(quote)):
            i += 1
            while i < len(lines):
                block.append(lines[i])
                if lines[i].rstrip().endswith(quote):
                    break
                i += 1
        entries.append((m.group(1), block))
        i += 1
    return entries


def write_env(example_path: Path, out_path: Path, values: dict[str, Any], rename: Renamer) -> None:
    """Writes out_path from the example, each key's value from config.yaml (check_envs has made
    sure every key has one)."""
    given = {rename(key): value for key, value in values.items()}
    out: list[str] = []
    for key, lines in env_entries(example_path.read_text()):
        out += lines if key is None else [f"{key}={env_value(given[key])}"]
    out_path.write_text("\n".join(out) + "\n")
    out_path.chmod(0o600)                        # passwords and keys live here


def env_files(root: Path, patch: bool) -> list[tuple[str, Path, Path]]:
    """(section, example, .env to write) for each example in this folder."""
    found = []
    for section, example_rel in ENV_FILES.items():
        if patch:
            folder, _, inner = example_rel.partition("/")
            example = root / folder / "new" / inner
            out = root / folder / Path(inner).name.removesuffix(".example")
        else:
            example = root / example_rel
            out = example.with_name(example.name.removesuffix(".example"))
        if example.exists():
            found.append((section, example, out))
    return found


def check_envs(root: Path, config: Config, settings: set[str], rename: Renamer, patch: bool) -> None:
    """Every key of every .env this run writes needs a value in config.yaml: a key that's missing,
    or given with no value (KEY: or KEY: null), stops prepare.py before anything changes. ""
    is a value: the key is written empty. A section left out of env: writes no .env."""
    for section in config.env:
        if section not in ENV_FILES:
            warn(f"{CONFIG_NAME}: env.{section} is not one of {', '.join(ENV_FILES)}; skipped")
    problems: list[str] = []
    for section, example, _ in env_files(root, patch):
        if section not in config.env:
            continue
        needed = {key for key, _ in env_entries(rename(without_blocks(example.read_text()))) if key}
        if section == "agent":
            needed |= {f.env for f in new_settings(config.inventory, settings, rename)}
        given = {rename(key): value for key, value in config.env[section].items()}
        for key in sorted(set(given) - needed):
            warn(f"{CONFIG_NAME}: env.{section}.{key} is not a setting of this release; skipped")
        missing = sorted(needed - set(given))
        empty = sorted(k for k in needed & set(given) if given[k] is None)
        if missing:
            problems.append(f"env.{section} is missing: {', '.join(missing)}")
        if empty:
            problems.append(f"env.{section} has no value for: {', '.join(empty)} "
                            f'(write "" for an empty value)')
    if problems:
        raise SystemExit(f"prepare: {CONFIG_NAME}:\n  " + "\n  ".join(problems))


def write_envs(root: Path, config: Config, rename: Renamer, patch: bool) -> None:
    for section, example, out in env_files(root, patch):
        if section not in config.env:
            print(f"no env.{section} in {CONFIG_NAME}: {out.relative_to(root.parent)} not written")
            continue
        write_env(example, out, config.env[section], rename)
        print(f"wrote {out.relative_to(root.parent)}")


# --- the two layouts ---

def stamp_text(sha: str, renames: dict[str, str]) -> str:
    return json.dumps({"config_sha256": sha, "rename": renames}, indent=2) + "\n"


def check_added_renames(before: dict[str, str], renames: dict[str, str]) -> None:
    """A prepared folder may take new renames, but every earlier one must stay as it was, and a
    new one may not overlap an earlier one (reload_phase1 after reload_phase finds nothing left
    to rename)."""
    changed = {k: v for k, v in before.items() if renames.get(k) != v}
    if changed:
        raise SystemExit(f"prepare: this folder was prepared with rename {before}; config.yaml now says "
                         f"{renames}. Renaming twice would mix names: start from a fresh unzip.")
    for added in renames.keys() - before.keys():
        overlap = [k for k in before if k in added or added in k]
        if overlap:
            raise SystemExit(f"prepare: rename.{added} overlaps rename.{overlap[0]}, which this folder "
                             f"was already prepared with: start from a fresh unzip.")


def previous_renames(root: Path) -> dict[str, str] | None:
    stamp = root / STAMP
    if not stamp.exists():
        return None
    return json.loads(stamp.read_text()).get("rename", {})


def trees(root: Path) -> list[Path]:
    """What gets renamed and generated: the whole folder, or each old/ and new/ in a patch."""
    if not (root / "PATCH").exists():
        return [root]
    return sorted(p for folder in root.iterdir() if folder.is_dir()
                  for p in (folder / "old", folder / "new") if p.is_dir())


def find_config_py(tree: Path) -> Path | None:
    candidate = tree / "agent" / "config.py"          # initial install
    if candidate.exists():
        return candidate
    candidate = tree / "config.py"                    # agent/new/ or agent/old/ in a patch
    if candidate.exists() and (tree / "agent").is_dir():
        return candidate
    return None


def main(argv: list[str]) -> int:
    args = argv[1:]
    check_only = "--check" in args
    root = HERE
    install: Path | None = None
    if "--install" in args:
        index = args.index("--install") + 1
        if index >= len(args):
            raise SystemExit("prepare: --install needs the install folder")
        install = Path(args[index]).resolve()
    if (root / "PATCH").exists():
        # A patch uses only the install folder's config.yaml, never a copy here.
        if install is None:
            raise SystemExit("prepare: this is a patch; apply it with its apply.sh, which prepares it "
                             "from the install folder's config.yaml:  bash apply.sh /path/to/install")
        if (root / CONFIG_NAME).exists():
            raise SystemExit(f"prepare: remove {root / CONFIG_NAME}; a patch uses only "
                             f"{install / CONFIG_NAME}")
        config_path = install / CONFIG_NAME
    elif install is not None:
        raise SystemExit("prepare: --install is only for a patch")
    else:
        config_path = root / CONFIG_NAME
    config, renames, sha = load_config(config_path)
    if install is not None:
        # Use the names the install has, so old/ matches it. The install takes any new rename
        # when its own prepare.py runs after the patch.
        installed = previous_renames(install)
        if installed is not None:
            renames = dict(sorted(((k, v) for k, v in installed.items() if k in PLACEHOLDERS),
                                  key=lambda item: len(item[0]), reverse=True))
    rename = Renamer(renames)

    before = previous_renames(root)
    if before is not None:
        check_added_renames(before, renames)

    config_pys = [p for t in trees(root) if (p := find_config_py(t))]
    config_pys.sort(key=lambda p: "new" not in p.relative_to(root).parts)     # a patch's new/ first
    if not config_pys:
        raise SystemExit("prepare: no agent/config.py here, so the agent settings can't be checked")
    settings = {rename(s) for s in agent_settings(config_pys[0])}
    commands = check_worker(config.worker, settings, rename)
    check_inventory(config.inventory, settings, rename)
    check_envs(root, config, settings, rename, patch=(root / "PATCH").exists())
    print(f"{CONFIG_NAME} is valid: {len(config.worker.endpoints)} metric commands, "
          f"{len(config.inventory.fields)} inventory fields, renames: {renames or 'none'}")
    if check_only:
        return 0

    for tree in trees(root):
        edited, moved = rename_tree(tree, rename)
        if edited or moved:
            print(f"renamed in {tree.relative_to(root.parent)}: {edited} files edited, {moved} paths renamed")
        for path in sorted(tree.rglob("*")):
            if path.is_file() and path.name not in SKIP_FILES and not (set(path.parts) & SKIP_DIRS):
                text = read_text(path)
                if text and "BEGIN generated: " in text:
                    for name in write_blocks(path, config, commands, settings, rename):
                        print(f"wrote {name} in {path.relative_to(root.parent)}")

    write_envs(root, config, rename, patch=(root / "PATCH").exists())

    stamp = stamp_text(sha, renames)
    (root / STAMP).write_text(stamp)
    if (root / "PATCH").exists():
        for folder in root.iterdir():
            if (folder / "apply.sh").exists():
                (folder / STAMP).write_text(stamp)
    given = {rename(k) for k in config.env.get("agent", {})}
    unset = sorted(f.env for f in new_settings(config.inventory, settings, rename) if f.env not in given)
    if unset:
        print(f"new agent settings from inventory: {', '.join(unset)}; set them in every agent .env "
              f"(or under env.agent in {CONFIG_NAME})")
    print("prepared")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
