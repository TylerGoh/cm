#!/usr/bin/env python3
"""Builds release zips into dist/. Versions are git tags v0, v1, ...

    python3 tools/make_patch.py              the next patch: newest tag vN to HEAD; tags HEAD v(N+1)
                                             and writes dist/cluster_manager-v(N+1)-patch.zip
    python3 tools/make_patch.py --rebuild vN the patch v(N-1) -> vN again, without tagging
    python3 tools/make_patch.py --initial    the initial install at the newest tag:
                                             dist/cluster_manager-initial.zip (VERSION inside)
    (push a new tag afterwards: git push origin vN)

The patch zip:
    cluster_manager-vN-patch/
      prepare.py  config.example.yaml  README.txt  PATCH
      agent/  cluster-manager/  file-server/  root/     each: apply.sh  PATCH  old/  new/  [DELETED.txt]
Each folder holds the files that changed under that service folder (root/ for the top level,
docs/, tools/), before (old/) and after (new/). The agent's generated-block files and the
.env.example of each included service are always carried, so a config.yaml change reaches the
boxes with the next patch. prepare.py renames and generates in old/ and new/; apply.sh checks the
box against old/ and copies new/ in.
"""
import io
import re
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "dist"
SERVICES = {                    # folder -> a file that must exist in its installed folder
    "agent": "agent/updates.py",
    "cluster-manager": "cluster_manager/client/cli.py",
    "file-server": "file_server/file_server.py",
}
ROOT_MARKER = "prepare.py"      # root/ applies to a whole install's top folder
ALWAYS_CARRIED = [              # every config.yaml-generated file
    "agent/agent/worker_client.py", "agent/agent/inventory.py", "agent/config.py", "agent/.env.example",
]
DATE = (2026, 1, 1, 0, 0, 0)


def git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True).stdout


def blob(rev: str, path: str) -> bytes | None:
    r = subprocess.run(["git", "-C", str(REPO), "show", f"{rev}:{path}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def mode(rev: str, path: str) -> int:
    out = git("ls-tree", rev, "--", path).decode().split()
    return 0o755 if out and out[0] == "100755" else 0o644


def versions() -> list[int]:
    return sorted(int(m.group(1)) for t in git("tag", "-l", "v*").decode().split()
                  if (m := re.fullmatch(r"v(\d+)", t)))


def add(z: zipfile.ZipFile, name: str, data: bytes, file_mode: int = 0o644) -> None:
    info = zipfile.ZipInfo(name, date_time=DATE)
    info.external_attr = file_mode << 16
    info.compress_type = zipfile.ZIP_DEFLATED
    z.writestr(info, data)


def split(path: str) -> tuple[str, str]:
    """(patch folder, path inside that service's installed folder)."""
    top, _, rest = path.partition("/")
    if top in SERVICES and rest:
        return top, rest
    return "root", path


def changed_paths(frm: str, to: str) -> list[str]:
    paths = set(git("diff", "--name-only", "--no-renames", frm, to).decode().split("\n")) - {""}
    touched = {split(p)[0] for p in paths}
    carried = list(ALWAYS_CARRIED)
    for service in touched & set(SERVICES):
        carried.append(f"{service}/.env.example")
    for path in carried:                                    # only what exists on both sides
        if blob(frm, path) is not None and blob(to, path) is not None:
            paths.add(path)
    return sorted(paths)


def readme(frm: str, to: str, folders: list[str], subjects: list[str]) -> str:
    lines = [
        f"cluster_manager {frm} -> {to}",
        "",
        *[f"  {s}" for s in subjects],
        "",
        "All at once, on your prepared install folder (the one holding config.yaml):",
        "       bash apply.sh /path/to/cluster_manager",
        "   It prepares this patch with that folder's config.yaml, checks every service, and applies",
        "   them all, or changes nothing if any check fails (--force replaces locally changed files).",
        "   Then commit and push the result.",
        "",
        "Or by hand, one box at a time:",
        "1. Copy your config.yaml into this folder (next to prepare.py) and run:",
        "       python3 prepare.py",
        "   It renames the terms and writes the generated code in every folder below, and writes",
        "   the .env of each service that has one here (at the top of that folder) to copy out.",
        "2. Copy each folder to the box that runs it and run its apply.sh on the installed folder:",
    ]
    for folder in folders:
        target = "/path/to/cluster_manager (a whole install's top folder)" if folder == "root" else f"/opt/{folder}"
        lines.append(f"       {folder}/:  bash apply.sh {target}")
    lines += [
        "   apply.sh checks every file is still at " + frm + " and changes nothing if one isn't (add",
        "   --force to replace it anyway). It backs up what it replaces to <target>/.patches/" + to + "/",
        "   and warns about .env keys the new version adds or no longer uses.",
        "3. Restart the services that were patched.",
        "",
    ]
    return "\n".join(lines)


APPLY_SH = r'''#!/usr/bin/env bash
# Applies this folder's patch to an installed folder:   bash apply.sh [--force] [--check] TARGET
# --check: only the checks below; changes nothing, exits 1 if applying would be refused.
# Run prepare.py (one level up) first. Checks every file in TARGET against old/ (outside the
# generated blocks) and changes nothing if any differs, unless --force. Backs up what it replaces
# to TARGET/.patches/<version>/, copies new/ in, removes what DELETED.txt lists, and warns about
# .env keys added or no longer used. Never touches a .env.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
val() { sed -n "s/^$1=//p" "$HERE/PATCH"; }
SERVICE=$(val service); FROM=$(val from); TO=$(val to); MARKER=$(val marker)
FORCE=0; CHECK=0; TARGET=""
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=1 ;;
    --check) CHECK=1 ;;
    -*) echo "usage: bash apply.sh [--force] TARGET" >&2; exit 2 ;;
    *) TARGET=$arg ;;
  esac
done
[[ -n "$TARGET" ]] || { echo "usage: bash apply.sh [--force] TARGET   (the installed $SERVICE folder)" >&2; exit 2; }
[[ -f "$HERE/PREPARED" ]] || { echo "$SERVICE $TO: not prepared; run prepare.py with your config.yaml first" >&2; exit 1; }
TARGET=$(cd "$TARGET" && pwd)
[[ -f "$TARGET/$MARKER" ]] || { echo "$SERVICE $TO: $TARGET has no $MARKER; is it the $SERVICE folder?" >&2; exit 1; }
echo "$SERVICE $FROM -> $TO on $TARGET"
if [[ -f "$TARGET/.patches/applied" ]] && grep -q "^$TO " "$TARGET/.patches/applied"; then
  echo "$TO is already applied"; exit 0
fi

strip() { sed '/--- BEGIN generated:/,/--- END generated:/d' "$1"; }
list() { [[ -d "$HERE/$1" ]] && (cd "$HERE/$1" && find . -type f | sed 's|^\./||' | sort) || true; }
mapfile -t OLD < <(list old)
mapfile -t NEW < <(list new)
mapfile -t GONE < <([[ -f "$HERE/DELETED.txt" ]] && grep -v '^$' "$HERE/DELETED.txt" || true)

DIFFER=()
for rel in "${OLD[@]}"; do
  if [[ ! -e "$TARGET/$rel" ]]; then DIFFER+=("$rel (missing)")
  elif ! cmp -s <(strip "$TARGET/$rel") <(strip "$HERE/old/$rel"); then DIFFER+=("$rel (changed)"); fi
done
for rel in "${NEW[@]}"; do
  if [[ ! -e "$HERE/old/$rel" && -e "$TARGET/$rel" ]] && ! cmp -s "$TARGET/$rel" "$HERE/new/$rel"; then
    DIFFER+=("$rel (exists, but $FROM doesn't have it)")
  fi
done
if [[ ${#DIFFER[@]} -gt 0 ]]; then
  echo "differs from $FROM:" >&2; printf '  %s\n' "${DIFFER[@]}" >&2
  if [[ $FORCE -eq 0 ]]; then echo "nothing changed; --force replaces them (backed up)" >&2; exit 1; fi
  echo "--force: replacing them" >&2
fi
if [[ $CHECK -eq 1 ]]; then echo "  ok to apply"; exit 0; fi

BACKUP="$TARGET/.patches/$TO"
for rel in "${OLD[@]}" "${NEW[@]}" "${GONE[@]}"; do
  [[ -z "$rel" || ! -e "$TARGET/$rel" || -e "$BACKUP/$rel" ]] && continue
  mkdir -p "$BACKUP/$(dirname "$rel")"; cp -p "$TARGET/$rel" "$BACKUP/$rel"
done
for rel in "${NEW[@]}"; do
  mkdir -p "$TARGET/$(dirname "$rel")"
  cp "$HERE/new/$rel" "$TARGET/$rel.patch-tmp"
  chmod --reference="$HERE/new/$rel" "$TARGET/$rel.patch-tmp"
  mv -f "$TARGET/$rel.patch-tmp" "$TARGET/$rel"
  echo "  patch $rel"
done
for rel in "${GONE[@]}"; do
  [[ -n "$rel" && -e "$TARGET/$rel" ]] && { rm -f "$TARGET/$rel"; echo "  del   $rel"; }
done
echo "$TO from $FROM $(date -u +%FT%TZ)" >> "$TARGET/.patches/applied"

# .env keys: the new example against the box's .env
keys() { sed -n 's/^\([A-Z][A-Z0-9_]*\)=.*/\1/p' "$1" | sort -u; }
for example in "${NEW[@]}"; do
  [[ "$example" == *.env.example ]] || continue
  live="$TARGET/${example%.example}"
  [[ -f "$live" ]] || continue
  missing=$(comm -23 <(keys "$TARGET/$example") <(keys "$live") | tr '\n' ' ')
  unused=$(comm -13 <(keys "$TARGET/$example") <(keys "$live") | tr '\n' ' ')
  [[ -n "$missing" ]] && echo "  ${example%.example} needs: $missing(see $example, or the .env prepare.py wrote)"
  [[ -n "$unused" ]] && echo "  ${example%.example} no longer uses: $unused(remove them: unknown keys stop the service)"
done
echo "$SERVICE $TO applied; old files are in $BACKUP."
[[ "$SERVICE" == root ]] || echo "Restart $SERVICE."
'''


APPLY_ALL_SH = r'''#!/usr/bin/env bash
# Applies the whole patch to a prepared install folder (the one holding prepare.py, config.yaml,
# agent/, cluster-manager/, file-server/), in one go:   bash apply.sh [--force] INSTALL_FOLDER
# 1. Prepares this patch with INSTALL_FOLDER/config.yaml (or a config.yaml already here), unless
#    it's prepared already. Needs python3 with pyyaml and pydantic ($PYTHON to choose another).
# 2. Checks every service folder first; if any would be refused, nothing changes (--force
#    replaces locally changed files, backed up).
# 3. Applies each one; a service folder missing from INSTALL_FOLDER is skipped.
# Then commit the result in your git repositories. .env files are never touched.
set -euo pipefail
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PYTHON=${PYTHON:-python3}
FORCE=(); TARGET=""
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=(--force) ;;
    -*) echo "usage: bash apply.sh [--force] INSTALL_FOLDER" >&2; exit 2 ;;
    *) TARGET=$arg ;;
  esac
done
[[ -n "$TARGET" ]] || { echo "usage: bash apply.sh [--force] INSTALL_FOLDER" >&2; exit 2; }
ROOT=$(cd "$TARGET" && pwd)
[[ -f "$ROOT/prepare.py" ]] || { echo "$ROOT has no prepare.py: it isn't an install folder" >&2; exit 1; }
TO=$(sed -n 's/^to=//p' "$HERE/PATCH")

if [[ ! -f "$HERE/PREPARED" ]]; then
  if [[ ! -f "$HERE/config.yaml" ]]; then
    [[ -f "$ROOT/config.yaml" ]] || { echo "no config.yaml in $ROOT or $HERE" >&2; exit 1; }
    cp "$ROOT/config.yaml" "$HERE/config.yaml"
  fi
  "$PYTHON" "$HERE/prepare.py"
fi

target_of() { if [[ "$1" == root ]]; then echo "$ROOT"; else echo "$ROOT/$1"; fi; }
FOLDERS=()
for dir in "$HERE"/*/; do
  folder=$(basename "$dir")
  [[ -f "$dir/apply.sh" ]] || continue
  if [[ ! -d "$(target_of "$folder")" ]]; then echo "skip $folder: no $folder/ in $ROOT"; continue; fi
  FOLDERS+=("$folder")
done

echo "== checking"
FAILED=0
for folder in "${FOLDERS[@]}"; do
  bash "$HERE/$folder/apply.sh" --check "${FORCE[@]}" "$(target_of "$folder")" || FAILED=1
done
if [[ $FAILED -ne 0 ]]; then echo "nothing changed" >&2; exit 1; fi

echo "== applying"
for folder in "${FOLDERS[@]}"; do
  bash "$HERE/$folder/apply.sh" "${FORCE[@]}" "$(target_of "$folder")"
done
echo "$TO applied to $ROOT. Commit it (git status shows what changed), then deploy and restart."
'''


def build_patch(frm: str, to: str) -> Path:
    subjects = git("log", "--format=%s", f"{frm}..{to}").decode().strip().splitlines()
    by_folder: dict[str, list[str]] = {}
    for path in changed_paths(frm, to):
        by_folder.setdefault(split(path)[0], []).append(path)
    folders = sorted(by_folder, key=lambda f: (f == "root", f))
    top = f"cluster_manager-{to}-patch"
    OUT.mkdir(exist_ok=True)
    out = OUT / f"{top}.zip"
    with zipfile.ZipFile(out, "w") as z:
        add(z, f"{top}/prepare.py", blob(to, "prepare.py") or b"", 0o755)
        add(z, f"{top}/config.example.yaml", blob(to, "config.example.yaml") or b"")
        add(z, f"{top}/README.txt", readme(frm, to, folders, subjects).encode())
        add(z, f"{top}/PATCH", f"from={frm}\nto={to}\n".encode())
        add(z, f"{top}/apply.sh", APPLY_ALL_SH.encode(), 0o755)
        for folder in folders:
            marker = ROOT_MARKER if folder == "root" else SERVICES[folder]
            add(z, f"{top}/{folder}/PATCH",
                f"service={folder}\nfrom={frm}\nto={to}\nmarker={marker}\n".encode())
            add(z, f"{top}/{folder}/apply.sh", APPLY_SH.encode(), 0o755)
            deleted = []
            for path in by_folder[folder]:
                inner = split(path)[1]
                old, new = blob(frm, path), blob(to, path)
                if old is not None:
                    add(z, f"{top}/{folder}/old/{inner}", old, mode(frm, path))
                if new is not None:
                    add(z, f"{top}/{folder}/new/{inner}", new, mode(to, path))
                else:
                    deleted.append(inner)
            if deleted:
                add(z, f"{top}/{folder}/DELETED.txt", ("\n".join(deleted) + "\n").encode())
    return out


def build_initial(version: str) -> Path:
    OUT.mkdir(exist_ok=True)
    out = OUT / "cluster_manager-initial.zip"
    archive = git("archive", "--format=zip", "--prefix=cluster_manager/", version)
    with zipfile.ZipFile(io.BytesIO(archive)) as src, zipfile.ZipFile(out, "w") as z:
        for info in src.infolist():
            if info.is_dir():
                continue
            add(z, info.filename, src.read(info), (info.external_attr >> 16) & 0o777 or 0o644)
        add(z, "cluster_manager/VERSION", f"{version}\n".encode())
    return out


def main(argv: list[str]) -> int:
    tags = versions()
    if "--initial" in argv:
        if not tags:
            sys.exit("no v0 tag yet: tag the commit first (git tag -a v0 -m v0)")
        print(build_initial(f"v{tags[-1]}"))
        return 0
    if "--rebuild" in argv:
        to = argv[argv.index("--rebuild") + 1]
        n = int(to.removeprefix("v"))
        if n < 1 or n not in tags:
            sys.exit(f"no patch version {to}")
        print(build_patch(f"v{n - 1}", to))
        return 0
    if git("status", "--porcelain").strip():
        sys.exit(f"uncommitted changes in {REPO}: commit first")
    if not tags:
        sys.exit("no v0 tag")
    frm, to = f"v{tags[-1]}", f"v{tags[-1] + 1}"
    if git("rev-parse", f"{frm}^{{commit}}") == git("rev-parse", "HEAD"):
        sys.exit(f"nothing new since {frm}")
    git("tag", "-a", to, "HEAD", "-m", f"{to}: patch from {frm}")
    print(build_patch(frm, to))
    print(f"tagged {to}; push it: git push origin {to}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
