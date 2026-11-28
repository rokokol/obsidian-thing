# Tools

Every vault serves its own endpoint, and the tools differ by what that vault allows. A read-only vault offers no write tool at all. A vault without a state directory offers no index tool. The client sees only the tools that its vault can run

| Appears when | Tools |
| --- | --- |
| Always | `allowed`, `agentsmd`, `skills_list`, `skill_read`, `memory_list`, `memory_read`, `read`, `stat`, `links_out`, `links_in`, `media` |
| `writable = true` | `memory_write`, `memory_delete`, `memory_index_write`, `write`, `update`, `rename` |
| `writable = true` and `trash_dir` | `delete` |
| `writable = true` and the Canvas integration | `canvas_edit` |
| `writable = true` and the Base integration | `base_edit` |
| The FSRS Vocabulary integration | `fsrs_list`, `fsrs_read`, `fsrs_history`, and `fsrs_edit` on a writable vault |
| `writable = true` and the Iconic integration | `iconic_set` |
| The Excalidraw integration | `excalidraw_render` |
| A state directory | `index`, `query`, `find`, `status` |
| A state directory and embeddings | `related`, and `map` for the plugin client |

Every tool name starts with `obsidian.`. Each one applies the vault allowlist, and a hidden path or a symbolic link is refused

## Vault and instructions

| Tool | Arguments | Result |
| --- | --- | --- |
| `obsidian.allowed` | none | `paths`: the allowed paths, relative to the vault root, with `.` for the whole vault |
| `obsidian.agentsmd` | none | The first readable file of the configured instruction list, as `path`, `content` and `revision` |
| `obsidian.skills_list` | none | The vault skills with their names, descriptions and `SKILL.md` paths |
| `obsidian.skill_read` | `name`, `path = "SKILL.md"` | One file of a skill. `path` stays inside the skill directory |
| `obsidian.memory_list` | none | The memory notes with their names, descriptions, types and revisions |
| `obsidian.memory_read` | `name` | One memory note without its extension, or `MEMORY.md` for the index |
| `obsidian.memory_write` | `name`, `content`, `expected_revision = null` | The new revision and path, and the updated line of the note in `MEMORY.md` |
| `obsidian.memory_delete` | `name`, `expected_revision` | `path`, `revision`, `trash`, and `index_revision` after the line leaves `MEMORY.md` |
| `obsidian.memory_index_write` | `content`, `expected_revision = null` | The new revision of `MEMORY.md` |

The agent paths come from the `agents` table. See [vault instructions, skills and memory](agents.md). `obsidian.memory_write` needs frontmatter that carries a description

`obsidian.memory_delete` hides a note rather than destroying it: the copy stays in the agent trash beside the notes, and the line leaves the index in the same call. `obsidian.memory_index_write` replaces the whole index and checks it first: a line that names no note, a note with no line and a note named twice each refuse the write, so the index cannot drift from the directory

## Reading

| Tool | Arguments | Result |
| --- | --- | --- |
| `obsidian.read` | `path` | `path`, `content` and `revision` of a UTF-8 file |
| `obsidian.stat` | `path` | `path`, `revision` and `size_bytes`, without the bytes |
| `obsidian.links_out` | `path` | `links`: each link in a Markdown note, Canvas or Base, with its target and status |
| `obsidian.links_in` | `path` | `links`: the occurrences that point to the file, and `skipped`: the visible documents the server could not inspect |
| `obsidian.media` | `path` | `media`: the local attachments that the document links, with `path`, `mime_type` and `size_bytes` |

A revision is the SHA-256 of the file bytes. The read limit is 1 MiB for a text file, and a non-UTF-8 file or one that holds a NUL byte is refused. See [links and attachments](graph.md) for the fields of a link and the meaning of each status

## Writing

Every write tool checks the revision first and replaces the file atomically. Run `obsidian.agentsmd` before the first edit of a session

| Tool | Arguments | Result |
| --- | --- | --- |
| `obsidian.write` | `path`, `content`, `expected_revision = null`, `all_warnings = false` | `path`, `revision`, and `warnings` when a rule or a checker reports one |
| `obsidian.update` | `path`, `old`, `new`, `expected_revision`, `all_warnings = false` | The same result, after one exact replacement |
| `obsidian.rename` | `path`, `new_name`, `expected_revision` | `path` of the new name, `previous_path`, `revision`, and `updated`: the documents whose links changed, each with its revision |
| `obsidian.delete` | `path`, `expected_revision` | `path`, `revision` and `trash_id` |

- **`write`** creates a file when `expected_revision` is absent or null. It refuses an existing path, so a replacement always names the revision that `obsidian.read` returned
- **`update`** replaces the one place where `old` occurs. The fragment must occur exactly once, and two overlapping matches refuse the edit
- **`rename`** keeps the directory and the file extension, and refuses an existing name. It updates the visible links to the old name. It refuses when a link cannot change safely, and it runs the write rules on every document it is about to change
- **`delete`** needs a configured trash directory. It refuses when a visible document would lose the target of a link, and it refuses a path that the allowlist names directly

A write also refuses when it would produce an invalid Canvas, Base or YAML frontmatter, when a configured rule reports an error, and when the checker rejects the change. The reply carries at most ten warnings unless you pass `all_warnings = true`. See [Markdown rules](vault-rules.md), [write checker](checker.md) and [structured edits](structured-edits.md)

## Structured edits

`obsidian.canvas_edit(path, expected_revision, changes, all_warnings = false)` applies a batch in order and validates the final document. One failed step leaves the file unchanged

| `op` | Fields | Effect |
| --- | --- | --- |
| `put_node` | `id`, `fields` | Add a node, or update the named fields of an existing node |
| `put_edge` | `id`, `fields` | Add an edge, or update the named fields of an existing edge |
| `remove_node` | `id` | Remove the node, with its edges in the same batch |
| `remove_edge` | `id` | Remove the edge |

`obsidian.base_edit(path, expected_revision, changes, all_warnings = false)` edits filters, formulas and views. It does not evaluate a formula

| `op` | Fields | Effect |
| --- | --- | --- |
| `set_filters` | `value`, `view` | Replace the filters of the Base, or of one view |
| `set_formula` | `name`, `value` | Add or replace a formula |
| `remove_formula` | `name` | Delete a formula |
| `put_view` | `name`, `fields` | Add a view, or update the named fields of an existing view |
| `remove_view` | `name` | Delete a view |

## Vault integrations

Each tool below needs its integration switch, and a writable vault where it changes a file. See [vault integrations](integrations.md)

| Tool | Arguments | Result |
| --- | --- | --- |
| `obsidian.iconic_set` | `path`, `changes` | `path`, `icon` and `color` after the change. `changes` holds `icon`, `color` or both, and null clears a value |
| `obsidian.fsrs_list` | `after = ""`, `limit = 50` | `dictionaries` after a path, with their columns and card counts, `skipped` and `next_after` |
| `obsidian.fsrs_read` | `path`, `offset = 0`, `limit = 50` | `cards` with their cells, FSRS state and review count. Rows start at zero. `next_offset` follows the page |
| `obsidian.fsrs_history` | `path`, `card_id = null`, `offset = 0`, `limit = 200` | `reviews` with grade and prior state for one card or the whole dictionary, and `ignored_lines` |
| `obsidian.fsrs_edit` | `path`, `expected_revision`, `changes`, `all_warnings = false` | The document after `create`, `update` or `delete` rows, applied in order |
| `obsidian.excalidraw_render` | `path`, `width = 1600` | A PNG image of a saved drawing, without Obsidian |

`obsidian.fsrs_edit` takes a row index for `update` and `delete`, and `cells` for `create` and `update`. It does not accept the schedule columns. Get the revision from `obsidian.fsrs_read`

## Search

The index tools need a state directory. The vector tools need an embedding profile on top. See [semantic search](semantic-search.md) for the index, the providers and the costs

| Tool | Arguments | Result |
| --- | --- | --- |
| `obsidian.index` | `path` | `path`, `revision` and `chunks`: the fragments the index holds for the file |
| `obsidian.query` | `text`, `limit = 10` | `matches`: one entry per file, ranked from text matches and vectors. `vector_unavailable` is set when the provider fails |
| `obsidian.find` | `tags`, `aliases`, `properties`, `alias_property = "aliases"`, `filename_regex`, `limit = 10` | `paths` in path order. Every filter must match, from one to 32 filters, and the tool needs no embeddings |
| `obsidian.status` | none | Index coverage, the embedding queue, the last full scan, the model profile and recorded costs |
| `obsidian.related` | `path`, `limit = 10`, `min_score = null`, `section_line = null`, `include_paths = null`, `mime_types = null`, `exclude_linked = false` | The nearest indexed neighbours, one entry per file, each with its best score and location |
| `obsidian.map` | The arguments of `related` | The same files in the same rank order, with coordinates on two axes |

`obsidian.map` is a plugin-only tool. The server hides it from `tools/list` and refuses it unless the client sends the header `X-Obsidian-Thing-Client: plugin`. The header selects the client interface. It does not replace the bearer token or change vault permissions

## Errors and warnings

A refused call returns an error text that names the cause, such as an unavailable path, a mismatched revision, a rule finding or a document that would become invalid. The tool result sets `is_error`, and the agent reads the message

A warning is advice that does not block the change: an old rule finding that the file already had, a finding in the file that the edit does not touch, or a message from the write checker. A rule error blocks the write. The checker blocks a write when it rejects the change, fails, times out, or prints more than 16 KiB

The server serializes MCP mutations to one path. It cannot lock out Obsidian or another writer, so a revision check protects against every change the server can see, and an unsaved buffer in Obsidian stays invisible until it is saved
