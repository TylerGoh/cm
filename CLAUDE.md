# Working in this repo

## Workflow

We pair program, one problem at a time.

1. **Propose.** Before editing, describe the approach and everything it touches, including anything outside the file in question.
2. **Discuss.** We tweak the proposal together. No edits yet.
3. **Edit.** Make the agreed change and leave it uncommitted, so it shows under Proposed in the editor.
4. **Commit.** Only when the owner agrees. Then remove the item from the todo list.

Don't edit several things at once, start the next item, or commit unprompted.

## The editor

`~/cluster_manager` is the main folder. `~/cm-viewer` is a tool folder that holds the editor. Nothing in it is project code.

- The editor (`~/cm-viewer/server.py`, http://127.0.0.1:7000) shows three tabs: **Current** (HEAD), **Proposed** (uncommitted edits, side by side) and **Todo**.
- The hooks in `.claude/settings.local.json` start it when a session begins (`~/cm-viewer/editor.sh start`) and stop it when the session ends.
- The todo list is `~/cm-viewer/todo.json`: `[{"title", "problem"}]`. Write each item as a problem statement in plain prose.
- "The editor" or "the cm viewer" always means this, never Claude Docs.

## Docs

- `docs/architecture.md`: how the system works.
- `docs/decisions.md`: what was decided and why. Settled outcomes go here.
- `docs/open-items.md`: unresolved questions and undesigned parts.

## Conventions

- New features work from defaults (`prepare.py`, `config.example.yaml`). They never need `config.yaml` edits.
- Before building anything that changes what gets sent or shown (Elastic documents, inventory records), show a sample of the output first.
- Config classes keep section headers only, with no comment per field.
- Releases: tag and build a patch only when asked.
