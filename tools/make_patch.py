#!/usr/bin/env python3
"""Builds the next version's patch for this repository.

    python3 tools/make_patch.py

Versions are git tags v0, v1, ... (v0 is the fresh install, cluster_manager-v0.zip). This takes
the newest tag vN to HEAD, tags HEAD v(N+1) and writes dist/cluster_manager-v(N+1)-patch.zip
(push the tag afterwards: git push origin v(N+1)):

    patch-vM/patch-vM.sh       the patch
    patch-vM/rename_term.sh    the repo's rename script at HEAD, for a box that has no copy
    patch-vM/RENAMES.txt       only when files are added or deleted

The script carries each changed file whole, before and after, gzipped. On the box it renames the
placeholders (rename_term.sh's checklist: the box's own copy at the project root, else the one
in the zip) in both, and in the file paths, so the check and the result use the box's names. It
checks every file is still at FROM (or already at TO) before it changes anything, backs the old
files up, and logs the patch. .env files and the box's rename_term.sh are never touched.
"""
import base64
import os
import gzip
import re
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(os.environ.get("CM_REPO", Path(__file__).resolve().parents[1]))   # this repository
OUT = Path(os.environ.get("CM_PATCH_OUT", REPO / "dist"))                     # git-ignored
KEEP = {"rename_term.sh"}            # the box's copy holds its checklist


def git(*args: str) -> bytes:
    return subprocess.run(["git", "-C", str(REPO), *args], check=True, capture_output=True).stdout


def blob(commit: str, path: str) -> bytes | None:
    r = subprocess.run(["git", "-C", str(REPO), "show", f"{commit}:{path}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


HEAD = r'''#!/usr/bin/env bash
# cluster_manager @FROM@ -> @TO@
@SUBJECTS@
#
# Unzip in the home folder and run:   bash patch-@NUM@/patch-@NUM@.sh
# (or give the project folder:        bash patch-@NUM@/patch-@NUM@.sh /path/to/cluster_manager)
#
# Placeholders (hypertree, program_log, ...) are renamed to this box's names, taken from the
# checklist in the project's own rename_term.sh, or else in the rename_term.sh next to this
# script. Every file is checked against @FROM@ with those names (a file already at @TO@ is
# skipped); if any check fails nothing changes. Old files go to <project>/.patches/@NUM@/.
# Only the folders this box has are patched. .env files and rename_term.sh are never touched.
set -euo pipefail
PATCH=@NUM@; FROM=@FROM@; TO=@TO@
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
MARKERS=("agent/updates.py" "cluster_manager/client/cli.py" "file_server/file_server.py")

if [[ $# -ge 1 ]]; then ROOT=$(cd "$1" && pwd); else
  mapfile -t ROOTS < <(for m in "${MARKERS[@]}"; do
      find "$PWD" -maxdepth 6 -path "*/.patches" -prune -o -type f -path "*/$m" -print 2>/dev/null; done |
    while read -r f; do d=$(dirname "$(dirname "$f")"); [[ "$f" == */cluster_manager/client/cli.py ]] && d=$(dirname "$d"); dirname "$d"; done | sort -u)
  if [[ ${#ROOTS[@]} -eq 0 ]]; then echo "patch $PATCH: no cluster_manager project under $PWD; pass its folder" >&2; exit 1; fi
  if [[ ${#ROOTS[@]} -gt 1 ]]; then echo "patch $PATCH: more than one project found, pass the one to patch:" >&2; printf "  %s\n" "${ROOTS[@]}" >&2; exit 1; fi
  ROOT=${ROOTS[0]}
fi
echo "cluster_manager $FROM -> $TO on $ROOT"
if [[ -f "$ROOT/.patches/applied" ]] && grep -q "^$TO " "$ROOT/.patches/applied"; then echo "$TO is already applied"; exit 0; fi

# --- this box's names, from rename_term.sh's checklist ---
NAMES=""
for src in "$ROOT/rename_term.sh" "$HERE/rename_term.sh"; do [[ -f "$src" ]] && { NAMES=$src; break; }; done
RENAMES=""
if [[ -n "$NAMES" ]]; then
  CHECKLIST=()
  eval "$(sed -n '/^CHECKLIST=(/,/^)/p' "$NAMES")"
  for entry in "${CHECKLIST[@]}"; do
    old=${entry%%=*}; new=${entry#*=}
    [[ -z "$new" || "$new" == "$old" ]] && continue
    [[ "$new" =~ ^[a-z][a-z0-9_]*$ ]] || { echo "patch $PATCH: bad name for $old in $NAMES: $new" >&2; exit 1; }
    RENAMES+="${RENAMES:+,}$old=$new"
  done
  echo "names from $NAMES: ${RENAMES:-none filled in, nothing renamed}"
else
  echo "no rename_term.sh found: nothing renamed"
fi
export RENAMES
# Same forms as rename_term.sh: snake, UPPER, Camel, words, Capitalised words; longest first.
ren() {
  if [[ -z "$RENAMES" ]]; then cat; return; fi
  perl -pe 'BEGIN { sub forms { my @w = split /_/, shift;
      return (join("_", @w), uc join("_", @w), join("", map { ucfirst } @w), join(" ", @w), ucfirst join(" ", @w)); }
    for my $p (split /,/, $ENV{RENAMES}) { my ($o, $n) = split /=/, $p; my @of = forms($o); my @nf = forms($n); my %m;
      for my $i (0 .. $#of) { $m{$of[$i]} //= $nf[$i]; }
      my $re = join "|", map { quotemeta } sort { length $b <=> length $a } keys %m; push @R, [qr/($re)/, \%m]; } }
    for my $r (@R) { my ($re, $m) = @$r; s/$re/$m->{$1}/g; }'
}
hash_of() { if [[ -e "$1" ]]; then sha256sum "$1" | cut -d" " -f1; else echo -; fi; }
# content old|new PATH: the file at FROM or TO, as stored here (fails if it doesn't exist there)
content() {
  case "$1:$2" in
@CASES@
    *) return 1 ;;
  esac
}
renamed_hash() { if content "$1" "$2" >/dev/null 2>&1; then content "$1" "$2" | ren | sha256sum | cut -d" " -f1; else echo -; fi; }

FILES=(
@FILES@
)
TODO=(); FAIL=0
for path in "${FILES[@]}"; do
  if [[ "${path##*/}" == .env || "${path##*/}" == rename_term.sh ]]; then echo "  keep  $path (never patched)"; continue; fi
  here=$(printf '%s' "$path" | ren); top=${here%%/*}
  if [[ "$here" == */* && ! -d "$ROOT/$top" ]]; then echo "  skip  $here (no $top/ on this box)"; continue; fi
  # top-level files (README.md, .gitignore) only in a full project, never in a bare home folder
  if [[ "$here" != */* && ! -f "$ROOT/rename_term.sh" ]]; then echo "  skip  $here (not a full project)"; continue; fi
  have=$(hash_of "$ROOT/$here"); old=$(renamed_hash old "$path"); new=$(renamed_hash new "$path")
  if [[ "$have" == "$new" ]]; then echo "  done  $here (already up to date)"
  elif [[ "$have" == "$old" ]]; then TODO+=("$path")
  else echo "  FAIL  $here differs from $FROM (changed locally, or different names?)" >&2; FAIL=1; fi
done
if [[ $FAIL -ne 0 ]]; then echo "patch $PATCH: nothing changed" >&2; exit 1; fi

BACKUP="$ROOT/.patches/$PATCH"; mkdir -p "$BACKUP"
for path in "${TODO[@]}"; do
  here=$(printf '%s' "$path" | ren); dst="$ROOT/$here"
  if [[ -e "$dst" ]]; then mkdir -p "$BACKUP/$(dirname "$here")"; cp -p "$dst" "$BACKUP/$here"; fi
  if content new "$path" >/dev/null 2>&1; then
    mkdir -p "$(dirname "$dst")"
    content new "$path" | ren > "$dst.patch-tmp"
    if [[ -e "$dst" ]]; then chmod --reference="$dst" "$dst.patch-tmp"; fi
    mv -f "$dst.patch-tmp" "$dst"; echo "  patch $here"
  else
    rm -f "$dst"; echo "  del   $here"
  fi
done
echo "$TO from $FROM $(date -u +%FT%TZ)" >> "$ROOT/.patches/applied"
echo "patch $PATCH applied; old files are in $BACKUP. Restart the services this box runs."
'''


def main() -> None:
    if git("status", "--porcelain").strip():
        sys.exit(f"uncommitted changes in {REPO}: commit first")
    tags = [int(m.group(1)) for t in git("tag", "-l", "v*").decode().split() if (m := re.fullmatch(r"v(\d+)", t))]
    if not tags:
        sys.exit("no v0 tag")
    last = max(tags)
    frm, to, num = f"v{last}", f"v{last + 1}", f"v{last + 1}"
    if git("rev-parse", f"{frm}^{{commit}}") == git("rev-parse", "HEAD"):
        sys.exit(f"nothing new since {frm}")
    subjects = git("log", "--format=%s", f"{frm}..HEAD").decode().strip().splitlines()
    git("tag", "-a", to, "HEAD", "-m", f"{to}: patch from {frm}")

    renames: list[str] = []
    paths: list[str] = []
    for line in git("diff", "--name-status", "--no-renames", frm, to).decode().splitlines():
        status, path = line.split("\t")
        if status == "D":
            renames.append(f"deleted {path}")
        elif status == "A":
            renames.append(f"added   {path}")
        paths.append(path)

    cases = []
    for path in paths:
        for kind, commit in (("old", frm), ("new", to)):
            data = blob(commit, path)
            if data is not None:
                enc = base64.encodebytes(gzip.compress(data, mtime=0)).decode()
                cases.append(f'    "{kind}:{path}") base64 -d <<\'__PATCH_EOF__\' | gunzip\n{enc}__PATCH_EOF__\n    ;;')

    script = (HEAD.replace("@NUM@", num).replace("@FROM@", frm).replace("@TO@", to)
              .replace("@SUBJECTS@", "\n".join(f"#   {s}" for s in subjects))
              .replace("@CASES@", "\n".join(cases))
              .replace("@FILES@", "\n".join(f'  "{p}"' for p in paths)))

    out = OUT / f"cluster_manager-{num}-patch.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in ((f"patch-{num}.sh", script.encode()), ("rename_term.sh", blob(to, "rename_term.sh"))):
            info = zipfile.ZipInfo(f"patch-{num}/{name}", date_time=(2026, 1, 1, 0, 0, 0))
            info.external_attr = 0o755 << 16
            z.writestr(info, data)
        if renames:
            z.writestr(f"patch-{num}/RENAMES.txt", "\n".join(renames) + "\n")
    print(out)


if __name__ == "__main__":
    main()
