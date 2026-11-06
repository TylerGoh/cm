#!/usr/bin/env python3
"""Prepares this folder from config.yaml: renames the placeholder terms and writes the generated
blocks (the worker contract, the inventory fields and their agent settings).

    cp /path/to/your/config.yaml .      # next to this script
    python3 prepare.py                  # needs pyyaml and pydantic (pip install pyyaml pydantic)
    python3 prepare.py --check          # validates config.yaml, changes nothing

It works on both kinds of folder it ships in:
  - the initial install (cluster_manager/: agent/, cluster-manager/, file-server/, ...)
  - a patch (cluster_manager-vN-patch/: a PATCH file, and <folder>/old/ and <folder>/new/ copies)

Running it again with the same config.yaml is safe: the renames are already done and the blocks
are written again. A different `rename:` in a folder already prepared is refused; unzip again.
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

try:
    import yaml
    from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
except ImportError as missing:
    sys.exit(f"prepare: {missing.name} is missing; install it with: pip install pyyaml pydantic")

HERE = Path(__file__).resolve().parent
CONFIG_NAME = "config.yaml"
STAMP = "PREPARED"

# Placeholders this version knows. A rename: key outside this list is skipped with a warning.
PLACEHOLDERS = ("hypertree", "program_log", "reload_phase", "etc_dir")
SNAKE_CASE = re.compile(r"^[a-z][a-z0-9_]*$")
CONSTANT_CASE = re.compile(r"^[A-Z][A-Z0-9_]*$")
ENV_KEY = re.compile(r"^[A-Z][A-Z0-9_]*$")

# Keys every Elastic document already carries (agent/agent/metrics_exporter.py DOCUMENT_KEYS and
# agent/agent/inventory.py RECORD_KEYS); neither a metric field nor an inventory field may take one.
METRICS_DOCUMENT_KEYS = frozenset({"at", "running_version", "updating", "hostname", "ipv4"})
INVENTORY_RECORD_KEYS = frozenset({"at", "hostname", "ipv4"})
FEED_SOURCES = ("process_status", "updating", "running_version", "busy")

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
    version_command: str                # answers {"version": "..."}; also the health ping
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
    source: str | None = None           # a feed value: process_status, updating, running_version, busy
    env: str | None = None              # or an agent .env key; a new one is added to config.py
    type: SettingType = SettingType.STR # for a new env key
    description: str | None = None

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
    def _env(cls, value: str | None) -> str | None:
        if value is not None and not ENV_KEY.match(value):
            raise ValueError(f"{value!r} must be an UPPER_CASE .env key")
        return value

    @model_validator(mode="after")
    def _one_source(self) -> "InventoryFieldSpec":
        if (self.source is None) == (self.env is None):
            raise ValueError(f"{self.field}: give exactly one of source: or env:")
        if self.source is not None and self.source not in FEED_SOURCES:
            raise ValueError(f"{self.field}: source {self.source!r} is not one of {', '.join(FEED_SOURCES)}")
        return self


DEFAULT_INVENTORY = [
    {"field": "process_status", "source": "process_status", "description": "None while not attached"},
    {"field": "updating", "source": "updating"},
    {"field": "running_version", "source": "running_version", "description": "None unless RUNNING"},
]


class InventorySpec(Lenient):
    fields: list[InventoryFieldSpec]


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
    inventory: InventorySpec = InventorySpec.model_validate({"fields": DEFAULT_INVENTORY})
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


def load_config(path: Path) -> tuple[Config, dict[str, str], str]:
    """The config, the renames to apply (placeholder -> name), and the file's sha256."""
    if not path.exists():
        raise SystemExit(f"prepare: no {path.name} next to prepare.py; copy yours into {path.parent}")
    raw_bytes = path.read_bytes()
    try:
        raw = yaml.safe_load(raw_bytes)
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

BLOCK = re.compile(r"^(?P<indent>[ \t]*)# --- BEGIN generated: (?P<name>[a-z ]+?)(?: \(.*\))? ---[ \t]*\n"
                   r"(?P<body>.*?)"
                   r"^[ \t]*# --- END generated: (?P=name) ---[ \t]*$", re.S | re.M)


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
    stem = re.sub(r"[^A-Za-z0-9]+", "_", re.sub(r"\{[^}]*\}", "", endpoint.command)).strip("_").upper()
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
        "# Metric commands: each answers one flat JSON object.",
    ]
    for endpoint in worker.endpoints:
        b.lines.append(f"{b.value(endpoint_constant(endpoint))} = MetricSource({b.value(quoted(commands[endpoint.command]))})")
    b.lines += [
        "",
        "",
        "class WorkerMetrics(BaseModel):",
        '    """One flat reading of the worker at one instant. Every field declares a MetricType,',
        '    a MetricSource and the name it has in that command\'s answer (validation_alias)."""',
        "    model_config = ConfigDict(frozen=True)",
    ]
    for endpoint in worker.endpoints:
        b.lines += ["", f"    # {b.value(endpoint.command)}"]
        for metric in endpoint.metrics:
            kind = "MetricType.COUNTER" if metric.type is MetricKind.COUNTER else "MetricType.GAUGE"
            line = (f"    {b.value(f'{metric.field}:'.ljust(width))} Annotated[float, {kind}, "
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
        if f.env is not None and rename(f.env.lower()) not in settings and f.env not in seen:
            seen.add(f.env)
            out.append(f)
    return out


def check_inventory(inventory: InventorySpec, settings: set[str], rename: Renamer) -> None:
    names = [f.field for f in inventory.fields]
    repeated = sorted({n for n in names if names.count(n) > 1})
    if repeated:
        raise SystemExit(f"prepare: inventory field repeated: {', '.join(repeated)}")
    for f in inventory.fields:
        if f.env is not None and rename(f.env.lower()) in settings and f.type is not SettingType.STR:
            warn(f"inventory {f.field}: {f.env} is an existing agent setting; its type there is kept")


def inventory_block(inventory: InventorySpec, settings: set[str], rename: Renamer) -> str:
    b = Block(rename)
    b.lines.append("INVENTORY_FIELDS: tuple[FeedField | SettingField, ...] = (")
    for f in inventory.fields:
        if f.source is not None:
            line = f"    FeedField({b.value(quoted(f.field))}, {b.value(quoted(f.source))}),"
        else:
            setting = rename(f.env.lower()) if rename(f.env.lower()) in settings else f.env.lower()
            line = f"    SettingField({b.value(quoted(f.field))}, {b.value(quoted(setting))}),"
        if f.description:
            line += f"    # {b.value(f.description)}"
        b.lines.append(line)
    b.lines += [")", ""]
    return b.text()


def config_py_block(inventory: InventorySpec, settings: set[str], rename: Renamer, indent: str) -> str:
    lines = []
    for f in new_settings(inventory, settings, rename):
        lines.append(f"{indent}{f.env.lower()}: {f.type.value}".ljust(41)
                     + f"# sent to the host index as {f.field}")
    return "\n".join(lines) + ("\n" if lines else "")


def env_example_block(inventory: InventorySpec, settings: set[str], rename: Renamer) -> str:
    lines = []
    for f in new_settings(inventory, settings, rename):
        note = f" ({f.description})" if f.description else ""
        lines.append(f"# {f.type.value}, sent to the host index as {f.field}{note}")
        lines.append(f"{f.env}=")
    return "\n".join(lines) + ("\n" if lines else "")


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


def write_env(example_path: Path, out_path: Path, values: dict[str, Any], rename: Renamer,
              section: str) -> list[str]:
    """Writes out_path from the example: each key's value from config.yaml, or empty to fill in.
    Returns the keys left empty."""
    given = {rename(key): value for key, value in values.items()}
    entries = env_entries(example_path.read_text())
    known = {key for key, _ in entries if key}
    for key in sorted(set(given) - known):
        warn(f"config.yaml: env.{section}.{key} is not a setting in {example_path.name} here; skipped")
    out: list[str] = []
    empty: list[str] = []
    for key, lines in entries:
        if key is None:
            out += lines
        elif key in given:
            out.append(f"{key}={env_value(given[key])}")
        else:
            out.append(f"{key}=")
            empty.append(key)
    out_path.write_text("\n".join(out) + "\n")
    out_path.chmod(0o600)                        # passwords and keys live here
    return empty


def write_envs(root: Path, config: Config, rename: Renamer, patch: bool) -> None:
    for section in config.env:
        if section not in ENV_FILES:
            warn(f"config.yaml: env.{section} is not one of {', '.join(ENV_FILES)}; skipped")
    for section, example_rel in ENV_FILES.items():
        values = config.env.get(section, {})
        if patch:
            folder, _, inner = example_rel.partition("/")
            example = root / folder / "new" / inner
            out = root / folder / Path(inner).name.removesuffix(".example")
        else:
            example = root / example_rel
            out = example.with_name(example.name.removesuffix(".example"))
        if not example.exists():
            continue
        empty = write_env(example, out, values, rename, section)
        where = out.relative_to(root.parent)
        print(f"wrote {where}" + (f"; fill in: {', '.join(empty)}" if empty else ""))


# --- the two layouts ---

def stamp_text(sha: str, renames: dict[str, str]) -> str:
    return json.dumps({"config_sha256": sha, "rename": renames}, indent=2) + "\n"


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
    check_only = "--check" in argv[1:]
    root = HERE
    config, renames, sha = load_config(root / CONFIG_NAME)
    rename = Renamer(renames)

    before = previous_renames(root)
    if before is not None and before != renames:
        raise SystemExit(f"prepare: this folder was prepared with rename {before}; config.yaml now says "
                         f"{renames}. Renaming twice would mix names: start from a fresh unzip.")

    config_pys = [p for t in trees(root) if (p := find_config_py(t))]
    config_pys.sort(key=lambda p: "new" not in p.relative_to(root).parts)     # a patch's new/ first
    if not config_pys:
        raise SystemExit("prepare: no agent/config.py here, so the agent settings can't be checked")
    settings = {rename(s) for s in agent_settings(config_pys[0])}
    commands = check_worker(config.worker, settings, rename)
    check_inventory(config.inventory, settings, rename)
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
                if text and "--- BEGIN generated: " in text:
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
    print("prepared" + (": copy each folder to its box and run its apply.sh" if (root / "PATCH").exists() else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
