#!/usr/bin/env bash
# Renames the placeholder names in the three projects to their real names: inside every text
# file, and in file and folder names. Only the placeholders in the checklist below can be renamed.
#
#   ./rename_term.sh --dry-run      lists what would change
#   ./rename_term.sh                renames
#
# Fill in the real name for each placeholder in CHECKLIST, then run it. A placeholder left empty
# is skipped. Real names are snake_case (lowercase letters, digits and underscores), and every
# form of a name is renamed in one pass:
#
#   snake_case    program_log      ->  app_log
#   UPPER_CASE    PROGRAM_LOG      ->  APP_LOG          (env keys: WORKER_PROGRAM_LOG_PATH)
#   CamelCase     ProgramLog       ->  AppLog
#   words         program log      ->  app log          (prose in comments and docs)
#   Capitalised   Program log      ->  App log
#
# Matches inside longer names are renamed too (hypertree_dir, fetch_hypertree, hypertrees).
# Run it on a fresh copy, before filling in any .env, so the .env.example keys already carry the
# real names. Renaming hypertree changes the wire (hypertree_get_url, hypertree_upload_url) and
# the file server's paths and stored file names, so reinstall all three services together.
# Skips .git, virtualenvs, caches, build output, binary files and this script.
set -euo pipefail

# --- the checklist: placeholder=real name ---
CHECKLIST=(
    "hypertree="
    "etc_dir="
    "program_log="
)

usage() {
    awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"
}

dry_run=0
case "${1:-}" in
    "") ;;
    --dry-run) dry_run=1 ;;
    *) usage; exit 2 ;;
esac

# --- read and check the checklist ---
olds=()
news=()
placeholders=()
for entry in "${CHECKLIST[@]}"; do
    placeholders+=("${entry%%=*}")
done
for entry in "${CHECKLIST[@]}"; do
    old="${entry%%=*}"
    new="${entry#*=}"
    [[ -z "$new" ]] && continue
    if [[ ! "$new" =~ ^[a-z][a-z0-9_]*$ ]]; then
        echo "rename_term: real name for $old, '$new', must be snake_case (a-z, 0-9, _), starting with a letter" >&2
        exit 2
    fi
    if [[ "$new" == "$old" ]]; then
        echo "rename_term: real name for $old is the placeholder itself" >&2
        exit 2
    fi
    for placeholder in "${placeholders[@]}"; do
        if [[ "$new" == "$placeholder" ]]; then
            echo "rename_term: real name for $old, '$new', is another placeholder" >&2
            exit 2
        fi
    done
    olds+=("$old")
    news+=("$new")
done
if (( ${#olds[@]} == 0 )); then
    echo "rename_term: nothing to rename; fill in real names in CHECKLIST at the top of $0" >&2
    exit 2
fi

root="$(cd "$(dirname "$0")" && pwd)"            # this script sits at the top of the repository
self="$root/$(basename "$0")"
cd "$root"

# The perl that every pass shares: builds every form of OLD and NEW, and one regex over all of
# OLD's forms (longest first), so each match is replaced exactly once.
read -r -d '' forms <<'PERL' || true
    sub forms { my @w = split /_/, shift;
        return (join('_', @w), uc join('_', @w), join('', map { ucfirst } @w),
                join(' ', @w), ucfirst join(' ', @w)); }
    our @of = forms($ENV{OLD}); our @nf = forms($ENV{NEW}); our %map;
    for my $i (0 .. $#of) { $map{$of[$i]} //= $nf[$i]; }
    our $re = join '|', map { quotemeta } sort { length $b <=> length $a } keys %map;
PERL

prune=(-name .git -o -name .venv -o -name venv -o -name __pycache__ -o -name '*.egg-info'
       -o -name .pytest_cache -o -name node_modules -o -name build -o -name dist)

text_files() {
    find . \( "${prune[@]}" \) -prune -o -type f -print0 |
        while IFS= read -r -d '' file; do
            [[ "$(cd "$(dirname "$file")" && pwd)/$(basename "$file")" == "$self" ]] && continue
            grep -Iq . "$file" 2>/dev/null && printf '%s\0' "$file"
        done
}

# Renames one placeholder: contents first, then file and folder names (deepest first, so a
# folder is renamed after its contents).
rename_one() {
    export OLD="$1" NEW="$2"
    echo "=== $OLD -> $NEW"

    local changed=0 file
    while IFS= read -r -d '' file; do
        if (( dry_run )); then
            perl -ne "BEGIN { $forms } print \"\$ARGV:\$.: \$_\" if /(\$re)/" "$file"
        elif perl -ne "BEGIN { $forms } \$hit = 1 if /(\$re)/; END { exit(\$hit ? 0 : 1) }" "$file"; then
            perl -pi -e "BEGIN { $forms } s/(\$re)/\$map{\$1}/g" "$file"
            echo "edited  $file"
            changed=$((changed + 1))
        fi
    done < <(text_files)

    local renamed=0 path base new_base target
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

    if (( ! dry_run )); then
        echo "$OLD: $changed files edited, $renamed paths renamed"
    fi
}

for i in "${!olds[@]}"; do
    rename_one "${olds[$i]}" "${news[$i]}"
done

if (( dry_run )); then
    echo "dry run: nothing was changed"
else
    echo "done. Outside this tree, rename by hand: live .env keys on each machine, and files"
    echo "already stored on disk under an old name (for hypertree: <FILE_SERVER_ROOT>/<job_id>/hypertree)"
fi
