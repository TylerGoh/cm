"""Prints the three services' dependencies (and dev extras), one per line, for dev's pip install."""
import sys
import tomllib
from pathlib import Path

repo = Path(sys.argv[1])
deps: set[str] = set()
for project in ("agent", "cluster-manager", "file-server"):
    meta = tomllib.loads((repo / project / "pyproject.toml").read_text())["project"]
    deps.update(meta.get("dependencies", []))
    deps.update(meta.get("optional-dependencies", {}).get("dev", []))
print("\n".join(sorted(deps)))
