# Working in this repo

## Workflow

We pair program, one problem at a time.

1. **Propose.** Before editing, describe the approach and everything it touches, including anything outside the file in question.
2. **Discuss.** We tweak the proposal together. No edits yet.
3. **Edit.** Make the agreed change and leave it uncommitted, so it shows under Proposed in the editor.
4. **Commit.** Only when the owner agrees. Then remove the item from the todo list.

Don't edit several things at once, start the next item, or commit unprompted.

What goes into the release zip gets the owner's review. What doesn't (`CLAUDE.md`, `devenv/`, `.gitattributes`) needs no separate review: commit it with the change it belongs to.

A large item is split into numbered steps, agreed as one plan up front.

- Each step goes through Edit and Commit on its own.
- "Commit it and move on to step N" means commit this step, then edit the next one.
- Finish a step before it is committed: never commit a state that breaks the code (a renamed field still read elsewhere). If asked to commit a step that isn't finished, say what would break and ask first.
- If a request changes the agreed plan (scope, data that would be lost), explain what else it takes with it and ask before editing.
- Remove the todo card only when the whole item is done.

## The editor

`~/cluster_manager` is the main folder. `~/cm-viewer` is a tool folder that holds the editor. Nothing in it is project code.

- The editor (`~/cm-viewer/server.py`, http://127.0.0.1:7000) shows these tabs:
  - **Current**: HEAD.
  - **Proposed**: uncommitted edits, side by side.
  - **Todo**.
  - **Decisions**: `docs/decisions.md` at HEAD.
  - **Downloads**: every zip in `dist/`.
  - **Ignored**.
- The hooks in `.claude/settings.local.json` start it when a session begins (`~/cm-viewer/editor.sh start`) and stop it when the session ends.
- The todo list is `~/cm-viewer/todo.json`: `[{"title", "problem"}]`. Write each item as a problem statement in plain prose.
- "The editor" or "the cm viewer" always means this, never Claude Docs.

## Docs

- `docs/architecture.md`: how the system works.
- `docs/decisions.md`: what was decided and why. Settled outcomes go here, dated, with Why and Cost.
- `docs/open-items.md`: unresolved questions and undesigned parts.

## Conventions

- New features work from defaults (`prepare.py`, `config.example.yaml`). They never need `config.yaml` edits.
- Before building anything that changes what gets sent or shown (Elastic documents, inventory records), show a sample of the output first.
- Config classes keep section headers only, with no comment per field.
- No comments that only restate a general coding practice. Comments explain this code's own reasons.
- No code for one-off transitions (handing over from an old release's state or processes). The owner does them by hand on the boxes; say exactly what to run instead.

## Tools and schema changes

- `tools/` ships in the patch. Only `devenv/`, `.gitattributes` and `CLAUDE.md` are left out.
- Scripts that only Claude maintains, like one-off migrations, may use handwritten SQL. The codebase rule "no literal SQL" is for project code.
- A schema change gets a one-off script in `tools/` (for example `tools/migrate_2026_11.py`). The script runs on the daemon's box with the daemon's `.venv` Python, the daemon stopped. It refuses to run twice.
- Test it against a temporary Postgres from devenv's `pgserver` (`devenv/.venv/.../pgserver/pginstall/bin`), with the old schema built from the old commit.

## Releases

- Tag and build a patch only when asked.
- "Package it and send it to downloads": run `python3 tools/make_patch.py`. It tags HEAD `v(N+1)` and writes `dist/patch_cm-v(N+1).zip`, which the editor's Downloads tab serves. Nothing needs copying.
- Push the tag only when asked.
