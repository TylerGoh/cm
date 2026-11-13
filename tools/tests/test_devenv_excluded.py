"""devenv/ is dev-only: never in the initial zip (git archive honours export-ignore) or a patch."""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tools"))
import make_patch  # noqa: E402


def test_git_archive_skips_devenv():
    out = subprocess.run(["git", "check-attr", "export-ignore", "devenv", "devenv/dev",
                          "devenv/harness/env.py", "agent/config.py"],
                         cwd=ROOT, capture_output=True, text=True, check=True).stdout
    assert "devenv: export-ignore: set" in out
    assert "agent/config.py: export-ignore: unspecified" in out


def test_patches_skip_devenv():
    assert make_patch.is_dev_only("devenv/dev")
    assert make_patch.is_dev_only("devenv/harness/scenarios.py")
    assert make_patch.is_dev_only(".gitattributes")
    assert make_patch.is_dev_only("CLAUDE.md")
    assert not make_patch.is_dev_only("agent/config.py")
    assert not make_patch.is_dev_only("prepare.py")
