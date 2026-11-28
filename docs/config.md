# Configuration

One TOML file holds every vault and the server settings. The server reads it at start and refuses an invalid file with a message that names the setting. Start from [`config.example.toml`](../config.example.toml), which lists every key as a comment

```sh
obsidian-thing --config /absolute/path/to/config.toml
```

The example below serves one vault read-only, with search and no embedding provider

```toml
[vaults.notes]
root = "/srv/notes"
allowed = [".", "AGENTS.md"]
token_env = "OBSIDIAN_THING_NOTES_TOKEN"

[vaults.notes.embeddings]
enabled = false
state_dir = "/var/lib/obsidian-thing/notes"
```

## Top-level keys

| Key | Default | Meaning |
| --- | --- | --- |
| `vaults` | required | A table of vaults. The server needs at least one |
| `metrics_token_env` | absent | The environment variable that holds the bearer token of `/metrics`. Without it the route does not exist |
| `allowed_hosts` | `localhost`, `localhost:*`, `127.0.0.1`, `127.0.0.1:*`, `[::1]`, `[::1]:*` | The `Host` headers the transport accepts. Add the host name that your proxy forwards |
| `allowed_origins` | empty | The `Origin` headers the transport accepts. A browser client needs its own origin here |

The transport rejects a request whose `Host` header is not listed. A reverse proxy passes the public name through, so that name belongs in `allowed_hosts`

## A vault

The table name is the vault ID, which is the last part of the endpoint `/vaults/<vault_id>/mcp`. Use ASCII letters, digits, underscores and hyphens

| Key | Default | Meaning |
| --- | --- | --- |
| `root` | required | The vault directory. The server resolves it at start and refuses a missing path |
| `allowed` | required | The vault-relative paths that this vault serves. `.` allows the whole vault |
| `token_env` | required | The environment variable that holds this vault's bearer token |
| `writable` | `false` | Offer the write tools and the tools of a writable integration |
| `read_obsidian` | `true` | Permit `obsidian.read` and `obsidian.stat` on explicit paths under `.obsidian/` |
| `protect_frontmatter` | `false` | Refuse a Markdown write that makes valid YAML frontmatter invalid. Needs `writable` |
| `trash_dir` | absent | A private directory for `obsidian.delete`. Needs `writable` |

An entry of `allowed` must exist and must be a file or a directory. A file allows itself alone, and a directory allows its subtree. A hidden name and a symbolic link are refused, and so is a path that holds an empty segment, a backslash or a NUL byte

The token is a random secret that you set in the environment. It accepts ASCII letters, digits and `._~+/=-`. The token of a vault must differ from the metrics token. Use one token for each vault that needs separate access

`trash_dir` must be an absolute directory, one per vault, and it must not overlap a vault. The trash and the file must share a mount, so a move cannot fail across file systems. See [deployment](deployment.md#deletion)

An integration that needs `read_obsidian` or `writable` is refused when the vault does not have it. A checker, a checks table and `protect_frontmatter` are refused on a read-only vault

## Instructions, skills and memory

| Key | Default | Meaning |
| --- | --- | --- |
| `agents.instructions` | `AGENTS.md`, `GEMINI.md`, `CLAUDE.md` | The instruction files in priority order |
| `agents.skills_dir` | `.agents/skills` | The directory of the vault skills |
| `agents.memory_dir` | `.agents/memory` | The directory of the memory notes |

Each path is relative to the vault root and must use plain names. The server reaches the configured directories by name, and never through `obsidian.read`. See [vault instructions, skills and memory](agents.md)

## Integrations

| Key | Default | Meaning |
| --- | --- | --- |
| `integrations.canvas` | `true` | Canvas graph scans, write checks and structured edits |
| `integrations.bases` | `true` | Base graph scans, view anchors, write checks and structured edits |
| `integrations.media_gallery` | `false` | Media Gallery blocks in the graph |
| `integrations.iconic_offline` | `false` | Changes to Iconic file settings. Needs `read_obsidian` and `writable` |
| `integrations.fsrs_dictionary` | `false` | The FSRS Vocabulary tools. Needs `read_obsidian` |
| `integrations.excalidraw` | `false` | Offline Excalidraw PNG export |

A disabled integration does not scan or rewrite its references. The text tools still accept these files. See [vault integrations](integrations.md)

## Write checks

Each key of the `checks` table is independent and off by default. A value is a boolean, except the two quote keys, which take a pair of characters

| Key | Meaning |
| --- | --- |
| `default_quotes` | The quote pair of new text, for example `'""'` |
| `inner_quoter` | The pair for a quote inside a quote. It needs `default_quotes` |
| `trailing_periods` | Reject a new final period |
| `prevent_inline_tags` | Reject new inline tags in Markdown |
| `inline_code_equals` | Inline code must not start with an equals sign |
| `heading_math` | A math character in a heading is an error, so a heading formula is TeX |
| `unicode_math` | A Unicode math character in body text is a warning, and TeX is preferred |
| `link_math` | A formula inside a wikilink breaks its target or caption |
| `paragraph_lines` | Keep one paragraph on one line, as a warning |
| `callouts` | Reject an unknown callout marker |
| `broken_links` | Reject a new broken link |
| `bases` | Reject an invalid Base |
| `canvas` | Reject an invalid Canvas |

A finding that the current file already has returns a warning, so an old mistake never blocks an edit. See [Markdown rules](vault-rules.md)

## Write checker

| Key | Default | Meaning |
| --- | --- | --- |
| `checker.script` | required | The absolute path of the script. It must be a regular file outside every vault, trash and state directory |
| `checker.timeout_seconds` | `2` | Above zero and at most 30 |
| `checker.vault_context_properties` | empty | Up to 32 distinct YAML property names to send with a Markdown write |

The checker table needs a writable vault. See [write checker](checker.md)

## Indexing

| Key | Default | Meaning |
| --- | --- | --- |
| `indexing.exclude` | `*.tmp`, `*.swp`, `*~` | Patterns in `.gitignore` syntax, relative to the vault root |
| `indexing.respect_gitignore` | `false` | Also read the `.gitignore` files of the vault |
| `indexing.auto` | `false` | Scan the vault at start and at intervals. Needs a state directory |
| `indexing.scan_seconds` | `300` | From 1 to 86400 |

The index exclusions narrow search only. They do not change access, the graph or `obsidian.media`. See [semantic search](semantic-search.md#index-exclusions)

## Embeddings

The `embeddings` table creates the search index. A vault without the table offers no index tool

| Key | Default | Meaning |
| --- | --- | --- |
| `enabled` | `true` in a present table | Vector search. `false` keeps full-text search alone |
| `state_dir` | required | An absolute private directory, owned by the server user, one per vault, outside every vault and trash |
| `provider` | required with `enabled` | `ollama`, `lm_studio`, `openai` or `openrouter` |
| `model` | required with `enabled` | The model name, passed through unchanged |
| `dimensions` | required with `enabled` | From 1 to 8192. A vector of another size is refused |
| `profile_version` | absent | Change it when you replace a model under the same name |
| `section_chunks` | `true` | Split Markdown at headings. `false` splits by size alone |
| `min_chunk_words` | `8` | From 0 to 1000. A shorter fragment stays out of the vector index |
| `related_min_score` | `0.0` | The default threshold of `obsidian.related`, from −1 to 1 |
| `include_paths` | every allowed path | The vault-relative paths that embeddings may cover |
| `exclude_paths` | empty | Paths that embeddings never cover. They win over `include_paths` |
| `allowed_mime_types` | `text/markdown`, `text/plain` | The file types the index may send |
| `background` | `false` | Scheduled requests to the model |
| `schedule` | `0 3 * * *` | A five-field cron expression in UTC |

The state directory must be private: no permission for group or other, and owned by the user that runs the server. It holds `search.sqlite3`, which contains indexed note text, and `spend.sqlite3`, which holds the cost journal

### Provider keys

| Provider | Keys |
| --- | --- |
| `ollama` | `url`, which defaults to `http://127.0.0.1:11434`. The server adds `/api/embed` |
| `lm_studio` | `base_url`, which defaults to `http://127.0.0.1:1234/v1`, and `request_dimensions` |
| `openai` | `base_url`, `request_dimensions`, and `key_env` when the endpoint needs a token |
| `openrouter` | `route`, `key_env`, `zdr` (default `true`) and `data_collection` (default `deny`, or `allow`) |

### Remote sending

| Key | Default | Meaning |
| --- | --- | --- |
| `allow_cloud` | `false` | Permit a request to a remote endpoint |
| `include_paths` | empty | Required before the first remote request, because it names what may leave the machine |

HTTP is permitted at `127.0.0.1` and `[::1]` only. Every other endpoint needs HTTPS, and the server follows no redirect

### Costs

| Key | Default | Meaning |
| --- | --- | --- |
| `usd_per_million_tokens` | absent | The rate for text, in USD per million tokens |
| `daily_budget_usd` | absent | The cap of the local estimate for each UTC day |
| `usd_per_attachment_unit` | empty | The rate of each MIME type: per image, PDF page, second of audio or source video frame |

With a daily budget, a request whose cost has no estimate is refused, and so is every request after a call with unknown cost. See [embedding costs](semantic-search.md#embedding-costs)

## What the server refuses at start

- A missing `vaults` table, a vault ID outside the allowed characters, or a vault without `root`, `allowed` or `token_env`
- An allowed path that does not exist, or one that names a hidden path, a symbolic link or a non-regular file
- A token that is empty, holds a character outside the allowed set, or equals the metrics token
- `state_dir` that is relative, public, or owned by another user, and two vaults that share one
- A trash, state or checker path that overlaps a vault, the trash or another state directory
- `indexing.auto` without a state directory, and a check or a checker on a read-only vault
- An integration that needs `read_obsidian` or `writable` and does not have it
- A checks table with an unknown key, a non-boolean value, or `inner_quoter` without `default_quotes`
- An embedding provider outside the four, a model without dimensions, dimensions outside 1 to 8192, or an endpoint without HTTPS outside loopback
- An attachment rate for a MIME type that the server does not support

## The environment

| Variable | Purpose |
| --- | --- |
| The name in each `token_env` | That vault's bearer token |
| The name in `metrics_token_env` | The token of `/metrics` |
| The name in `embeddings.key_env` | The provider key of a remote endpoint |

The server passes no environment to a checker script except `PYTHONPATH` for its own package directories. Vault tokens never reach the checker
