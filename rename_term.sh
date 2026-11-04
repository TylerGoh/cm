#!/usr/bin/env bash
# Renames a term everywhere in the three projects: inside every text file, and in file and folder names.
#
#   ./rename_term.sh [--dry-run] OLD NEW
#
# OLD and NEW are snake_case (lowercase letters, digits and underscores). Every form of the term
# is renamed in one pass, so a NEW that contains OLD is safe:
#
#   snake_case    program_log      ->  app_log
#   UPPER_CASE    PROGRAM_LOG      ->  APP_LOG          (env keys: WORKER_PROGRAM_LOG_PATH)
#   CamelCase     ProgramLog       ->  AppLog
#   words         program log      ->  app log          (prose in comments and docs)
#   Capitalised   Program log      ->  App log
#
# For a one-word OLD, the snake_case form wins over the words form. Matches inside longer names
# are renamed too (hypertree_dir, fetch_hypertree, hypertrees). Examples:
#
#   ./rename_term.sh --dry-run hypertree speedtree    # list what would change
#   ./rename_term.sh hypertree speedtree
#   ./rename_term.sh program_log app_log
#
# Skips .git, virtualenvs, caches, build output, binary files and this script.
# Renaming hypertree changes the wire (hypertree_get_url, hypertree_upload_url) and the file
# server's paths and stored file names, so reinstall the agent, the daemon and the file server together.
set -euo pipefail

dry_run=0
if [[ "${1:-}" == "--dry-run" ]]; then
    dry_run=1
    shift
fi
if [[ $# -ne 2 ]]; then
    sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
fi
old="$1"
new="$2"
for term in "$old" "$new"; do
    if [[ ! "$term" =~ ^[a-z][a-z0-9_]*$ ]]; then
        echo "rename_term: '$term' must be snake_case (a-z, 0-9, _), starting with a letter" >&2
        exit 2
    fi
done
if [[ "$old" == "$new" ]]; then
    echo "rename_term: OLD and NEW are the same" >&2
    exit 2
fi

root="$(cd "$(dirname "$0")" && pwd)"            # this script sits at the top of the repository
self="$root/$(basename "$0")"
cd "$root"

# The perl that both modes share: builds every form of OLD and NEW, and one regex over all of
# OLD's forms (longest first), so each match is replaced exactly once.
read -r -d '' forms <<'PERL' || true
    sub forms { my @w = split /_/, shift;
        return (join('_', @w), uc join('_', @w), join('', map { ucfirst } @w),
                join(' ', @w), ucfirst join(' ', @w)); }
    our @of = forms($ENV{OLD}); our @nf = forms($ENV{NEW}); our %map;
    for my $i (0 .. $#of) { $map{$of[$i]} //= $nf[$i]; }
    our $re = join '|', map { quotemeta } sort { length $b <=> length $a } keys %map;
PERL
export OLD="$old" NEW="$new"

prune=(-name .git -o -name .venv -o -name venv -o -name __pycache__ -o -name '*.egg-info'
       -o -name .pytest_cache -o -name node_modules -o -name build -o -name dist)

text_files() {
    find . \( "${prune[@]}" \) -prune -o -type f -print0 |
        while IFS= read -r -d '' file; do
            [[ "$(cd "$(dirname "$file")" && pwd)/$(basename "$file")" == "$self" ]] && continue
            grep -Iq . "$file" 2>/dev/null && printf '%s\0' "$file"
        done
}

# --- contents ---
changed=0
while IFS= read -r -d '' file; do
    if (( dry_run )); then
        perl -ne "BEGIN { $forms } print \"\$ARGV:\$.: \$_\" if /(\$re)/" "$file"
    elif perl -ne "BEGIN { $forms } \$hit = 1 if /(\$re)/; END { exit(\$hit ? 0 : 1) }" "$file"; then
        perl -pi -e "BEGIN { $forms } s/(\$re)/\$map{\$1}/g" "$file"
        echo "edited  $file"
        changed=$((changed + 1))
    fi
done < <(text_files)

# --- file and folder names (deepest first, so a folder is renamed after its contents) ---
renamed=0
while IFS= read -r -d '' path; do
    base="$(basename "$path")"
    new_base="$(printf '%s' "$base" | perl -pe "BEGIN { $forms } s/(\$re)/\$map{\$1}/g")"
    [[ "$base" == "$new_base" ]] && continue
    target="$(dirname "$path")/$new_base"
    if (( dry_run )); then
        echo "rename  $path -> $target"
    elif [[ -e "$target" ]]; then
        echo "rename_term: $target already exists; left $path as it is" >&2
    else
        mv "$path" "$target"
        echo "renamed $path -> $target"
        renamed=$((renamed + 1))
    fi
done < <(find . -depth \( "${prune[@]}" \) -prune -o -mindepth 1 -print0)

if (( dry_run )); then
    echo "dry run: nothing was changed"
else
    echo "done: $changed files edited, $renamed paths renamed"
    echo "outside this tree, rename by hand: live .env keys on each machine, and files already"
    echo "stored on disk under the old name (for hypertree: <FILE_SERVER_ROOT>/<job_id>/hypertree)"
fi
