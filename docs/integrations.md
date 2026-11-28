# Vault integrations

Use the `integrations` table inside each vault configuration. See [the example configuration](../config.example.toml)

| Switch | Default | Effect |
| --- | --- | --- |
| `canvas` | `true` | Enable Canvas graph scans, write checks and structured edits |
| `bases` | `true` | Enable Base graph scans, view anchors, write checks and structured edits |
| `media_gallery` | `false` | Include Media Gallery blocks in the graph |
| `iconic_offline` | `false` | Enable changes to Iconic file settings |
| `fsrs_dictionary` | `false` | Enable FSRS Vocabulary tools |
| `excalidraw` | `false` | Enable offline Excalidraw PNG export |

A disabled integration does not scan or rewrite its references. The text tools still accept these files if the allowlist permits them

## Obsidian settings

`read_obsidian` defaults to `true`. It permits `obsidian.read` and `obsidian.stat` on explicit paths under `.obsidian/`. Other hidden paths and symlinks remain inaccessible. The graph and search do not scan `.obsidian`. The general write tools cannot change it

Plugin settings can contain credentials. Any client with the vault token can read these settings when `read_obsidian` is on. Set it to `false` if the client must not have that access. Some integrations require it to be on

## Iconic

Enable `iconic_offline` only for a writable vault with `read_obsidian` on. Close Obsidian on hosts that can write or sync the same Iconic settings. The server does not check whether Obsidian runs on another host

Call `obsidian.iconic_set` after you create a file or when you change its appearance. Pass the existing file `path` and `changes`, such as `{"icon":"lucide-book","color":"blue"}`. Omit a field to keep it. Set it to `null` to clear it

The tool changes only the named entry in the existing Iconic settings. It does not set an icon as part of `obsidian.write` or `obsidian.update`

## Excalidraw

Enable `excalidraw` to register `obsidian.excalidraw_render`. The tool takes an allowed vault path and an optional pixel `width`. It returns PNG data as MCP image content. It does not start Obsidian or write to the vault

The tool reads `.excalidraw` scenes and Markdown drawings with the `excalidraw-plugin` property. It reads both plain and compressed scene blocks. It rejects embedded files and unsupported elements instead of omitting them. The renderer approximates hand-drawn strokes and replaces Excalidraw fonts

For a Python installation, install `obsidian-thing[excalidraw]` and the system Cairo library. The Nix package includes the renderer and Cairo

## FSRS Vocabulary

Enable `fsrs_dictionary` and keep `read_obsidian` on. The server reads Markdown dictionaries from the allowlist and uses the default FSRS Vocabulary plugin directory under `.obsidian/plugins/`. It does not need a running Obsidian instance

| Tool | Result |
| --- | --- |
| `obsidian.fsrs_list` | List dictionary paths, revisions, content columns, row counts and mute flags. Use `next_after` for the next page |
| `obsidian.fsrs_read` | Read content and schedule state by `path`, `offset` and `limit`. Includes stability, difficulty, due date, last review and recall estimate |
| `obsidian.fsrs_history` | Read timestamped ratings for current cards in one dictionary. Filter by `card_id` if needed. Use `next_offset` for the next page |
| `obsidian.fsrs_edit` | Apply row changes in order at the revision from `fsrs_read`. This tool requires a writable vault |

Each dictionary uses the `fsrs-vocabulary` property and a `Words` table. The editor requires unique, nonempty column names and rejects surplus cells. It preserves text outside the table and rows that do not change

Pass `changes` with `op: create` and all content `cells`, `op: update` with a zero-based `row` and the `cells` to change, or `op: delete` with a `row`. Row indices refer to the table after prior changes in the same batch. New rows have no schedule. Updates preserve schedule values. Deletion removes the row but does not erase review logs

The recall estimate uses the stored stability and the FSRS-6 decay at `as_of`. The response gives the decay as a positive value `d`. For stability `S` and elapsed whole days `t`, the estimate is `(1 + (0.9 ** (-1/d) - 1) * t / S) ** -d`. It is a model estimate, not a measured recall rate. The server does not replay reviews, change schedules or fit weights

History merges journal files from different devices and deduplicates by card ID, timestamp and rating. Ratings are 1 for Again, 2 for Hard, 3 for Good and 4 for Easy. Missing logs and unfinished lines can leave history incomplete. Changes from Obsidian or a sync client do not share the server's write lock
