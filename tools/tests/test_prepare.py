"""prepare.py on a copy of this repository. Needs pyyaml and pydantic.
Run from the repository: python -m pytest tools/tests"""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
import prepare  # noqa: E402

EXAMPLE = yaml.safe_load((REPO / "config.example.yaml").read_text())


def tree(tmp_path: Path) -> Path:
    files = subprocess.run(["git", "-C", str(REPO), "ls-files", "-co", "--exclude-standard"],
                           check=True, capture_output=True, text=True).stdout.split()
    root = tmp_path / "cluster_manager"
    for rel in files:
        if rel.startswith("dist/") or not (REPO / rel).is_file():
            continue
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, root / rel)
    return root


def run(root: Path, config: dict, *args: str) -> subprocess.CompletedProcess[str]:
    (root / "config.yaml").write_text(yaml.safe_dump(config))
    return subprocess.run([sys.executable, str(root / "prepare.py"), *args],
                          capture_output=True, text=True)


def renamed_config(**changes) -> dict:
    config = yaml.safe_load(yaml.safe_dump(EXAMPLE))
    config["rename"] = {"hypertree": "route_map", "program_log": "app_log", "reload_phase": "reload_bind"}
    config.update(changes)
    return config


def test_forms():
    assert prepare.forms("program_log") == ["program_log", "PROGRAM_LOG", "ProgramLog", "program log", "Program log"]
    rename = prepare.Renamer({"hypertree": "route_map", "reload_phase": "reload_bind"})
    assert rename("fetch_hypertree HYPERTREE_DIR Hypertrees reload_phase1() RELOAD_PHASE2_COMMAND") == \
        "fetch_route_map ROUTE_MAP_DIR RouteMaps reload_bind1() RELOAD_BIND2_COMMAND"


@pytest.mark.parametrize("command, constant", [
    ("counters", "COUNTERS_ENDPOINT"),
    ("show%20counters", "SHOW_COUNTERS_ENDPOINT"),
    ("show counters", "SHOW_COUNTERS_ENDPOINT"),
    ("stats.queue", "STATS_QUEUE_ENDPOINT"),
    ("show%2Fstats?x={config.etc_dir}", "SHOW_STATS_X_ENDPOINT"),
])
def test_endpoint_names_come_from_the_decoded_command(command, constant):
    endpoint = prepare.EndpointSpec(command=command, metrics=[{"name": "a", "field": "a", "type": "gauge"}])
    assert prepare.endpoint_constant(endpoint) == constant


def test_example_config_reproduces_the_repo(tmp_path):
    root = tree(tmp_path)
    result = run(root, EXAMPLE)
    assert result.returncode == 0, result.stderr
    for rel in ["agent/agent/worker_client.py", "agent/agent/inventory.py", "agent/config.py", "agent/.env.example"]:
        assert (root / rel).read_text() == (REPO / rel).read_text(), rel


def test_renames_everything_and_runs_again(tmp_path):
    root = tree(tmp_path)
    assert run(root, renamed_config()).returncode == 0
    leftovers = [p for p in root.rglob("*") if p.is_file() and p.name not in prepare.SKIP_FILES
                 and "PROGRAM_LOG" in p.read_text(errors="ignore")]
    assert leftovers == []
    client = (root / "agent/agent/worker_client.py").read_text()
    assert "async def reload_bind1" in client and "RELOAD_BIND2_COMMAND" in client
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    assert run(root, renamed_config()).returncode == 0
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_other_names_on_a_prepared_folder_are_refused(tmp_path):
    root = tree(tmp_path)
    assert run(root, renamed_config()).returncode == 0
    other = renamed_config()
    other["rename"]["hypertree"] = "mesh"
    result = run(root, other)
    assert result.returncode != 0 and "fresh unzip" in result.stderr


def test_each_reload_phase_can_have_its_own_name():
    rename = prepare.Renamer({"reload_phase1": "prepare_bind", "reload_phase2": "switch_bind"})
    assert rename("reload_phase1() RELOAD_PHASE2_COMMAND ReloadPhase1") == \
        "prepare_bind() SWITCH_BIND_COMMAND PrepareBind"


def test_per_phase_names_win_over_reload_phase(tmp_path):
    root = tree(tmp_path)
    config = renamed_config()
    config["rename"]["reload_phase1"] = "prepare_bind"
    assert run(root, config).returncode == 0
    client = (root / "agent/agent/worker_client.py").read_text()
    assert "async def prepare_bind" in client and "async def reload_bind2" in client


def test_a_prepared_folder_takes_new_renames(tmp_path):
    root = tree(tmp_path)
    config = renamed_config()
    del config["rename"]["reload_phase"]
    assert run(root, config).returncode == 0
    config["rename"].update(reload_phase1="prepare_bind", reload_phase2="switch_bind")
    result = run(root, config)
    assert result.returncode == 0, result.stderr
    client = (root / "agent/agent/worker_client.py").read_text()
    assert "async def prepare_bind" in client and "SWITCH_BIND_COMMAND" in client
    assert "def reload_phase" not in client and "RELOAD_PHASE" not in client   # commands are values, never renamed


def test_a_new_rename_overlapping_an_earlier_one_is_refused(tmp_path):
    root = tree(tmp_path)
    assert run(root, renamed_config()).returncode == 0
    config = renamed_config()
    config["rename"]["reload_phase1"] = "prepare_bind"
    result = run(root, config)
    assert result.returncode != 0 and "overlaps rename.reload_phase" in result.stderr


def test_command_fields_are_checked_and_renamed(tmp_path):
    root = tree(tmp_path)
    config = renamed_config()
    config["worker"]["reload_phase2_command"] = "reload_app_phase2{config.hypertree_dir}"
    assert run(root, config).returncode == 0
    assert 'RELOAD_BIND2_COMMAND = "reload_app_phase2{config.route_map_dir}"' in \
        (root / "agent/agent/worker_client.py").read_text()

    config["worker"]["reload_phase2_command"] = "reload_app_phase2{config.no_such_dir}"
    result = run(tree(tmp_path / "b"), config)
    assert result.returncode != 0 and "no_such_dir" in result.stderr


@pytest.mark.parametrize("command", ["/leading", "stray{brace", "x{other.etc_dir}"])
def test_bad_commands_change_nothing(tmp_path, command):
    root = tree(tmp_path)
    config = renamed_config()
    config["worker"]["reload_phase1_command"] = command
    result = run(root, config)
    assert result.returncode != 0
    assert "hypertree" in (root / "agent/config.py").read_text()        # nothing renamed


def test_unknown_keys_are_skipped_with_a_warning(tmp_path):
    config = renamed_config(future={"x": 1})
    config["rename"]["future_term"] = "x"
    result = run(tree(tmp_path), config)
    assert result.returncode == 0, result.stderr
    assert "future is not known" in result.stderr and "future_term" in result.stderr


def example_env(section: str) -> dict:
    """Every key of a service's .env.example, with its example value as text."""
    path = REPO / prepare.ENV_FILES[section]
    return {k: "x" for k, _ in prepare.env_entries(prepare.without_blocks(path.read_text())) if k}


def test_inventory_env_fields_become_settings_and_env_lines(tmp_path):
    root = tree(tmp_path)
    config = renamed_config()
    config["inventory"] = {"fields": [{"field": "state", "source": "process_status"},
                                      {"field": "rack", "env": "RACK", "type": "int"},
                                      {"field": "tree", "env": "HYPERTREE_DIR"}]}
    agent = example_env("agent")
    agent.update({"IPV4": "", "RACK": 7, "HYPERTREE_DIR": "/srv/tree", "WORKER_COMMAND": ["/w", "-a"],
                  "LOG_FORMAT": "%(asctime)s %(message)s"})
    config["env"] = {"agent": agent, "file-server": example_env("file-server")}
    result = run(root, config)
    assert result.returncode == 0, result.stderr
    assert "    rack: int" in (root / "agent/config.py").read_text()
    inventory = (root / "agent/agent/inventory.py").read_text()
    fields = inventory.split("# BEGIN generated: inventory fields")[1].split("# END generated")[0]
    assert fields == ('\nINVENTORY_FIELDS: dict[str, object] = {\n    "rack": config.rack,\n'
                      '    "tree": config.route_map_dir,\n}\n')               # state (a source: field) skipped
    assert "inventory field state (source: process_status) is no longer needed" in result.stderr
    env = (root / "agent/.env").read_text()
    assert "\nRACK=7\n" in env and "\nROUTE_MAP_DIR=/srv/tree\n" in env and "\nWORKER_APP_LOG_PATH=x\n" in env
    assert "\nWORKER_COMMAND='[\"/w\", \"-a\"]'\n" in env
    assert '\nLOG_FORMAT="%(asctime)s %(message)s"\n' in env
    assert "\nIPV4=\n" in env                                           # "" is written empty
    assert (root / "agent/.env").stat().st_mode & 0o777 == 0o600
    assert (root / "file-server/.env").exists()
    assert not (root / "cluster-manager/.env").exists()                # no env.cluster-manager: not written


@pytest.mark.parametrize("change, message", [
    (lambda env: env.pop("LOG_FORMAT"), "missing: LOG_FORMAT"),
    (lambda env: env.update(LOG_FORMAT=None), "no value for: LOG_FORMAT"),
    (lambda env: None, "missing: RACK"),                                 # a new inventory setting
])
def test_a_key_without_a_value_changes_nothing(tmp_path, change, message):
    root = tree(tmp_path)
    config = renamed_config()
    config["inventory"] = {"fields": [{"field": "rack", "env": "RACK", "type": "int"}]}
    agent = example_env("agent")
    if message != "missing: RACK":
        agent["RACK"] = 1
    change(agent)
    config["env"] = {"agent": agent}
    result = run(root, config)
    assert result.returncode != 0 and message in result.stderr
    assert "hypertree" in (root / "agent/config.py").read_text() and not (root / "agent/.env").exists()


def test_check_changes_nothing(tmp_path):
    root = tree(tmp_path)
    result = run(root, renamed_config(), "--check")
    assert result.returncode == 0
    assert not (root / "PREPARED").exists() and "hypertree" in (root / "agent/config.py").read_text()
