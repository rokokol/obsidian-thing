# Check writes before they reach disk

Set `checker.script` for a writable vault in [the example config](../config.example.toml). If you omit the checker table, the server does not run a script. In Docker, mount the script or its directory read-only and use its absolute path inside the container

The server runs the script with its Python interpreter, with no user site-packages, and with the package directories of the server. Every other environment variable, including vault tokens, is absent. Keep the script outside all vaults, trash and embedding state. Use only trusted code: the process has the server user's permissions and is not a sandbox

The script receives one UTF-8 JSON object on stdin. `operation` is `write` or `update`, and `path` is relative to the vault. `before` is null for a new file. `after` is the complete proposed text, even for a fragment edit. `links` lists the wikilinks of the proposed text with their resolution: `raw`, `target`, `anchor`, `embed`, `resolved` paths, `self` and `anchor_ok`. The list holds at most 200 links and five destinations for each link

```json
{"version":1,"operation":"update","path":"Notes/Example.md","before":"Old text\n","after":"New text\n","links":[{"raw":"[[Other]]","target":"Other","anchor":"","embed":false,"resolved":["Notes/Other.md"],"self":false,"anchor_ok":true}]}
```

Exit with status zero and write one UTF-8 JSON response to stdout. Use `{"decision":"allow"}` to accept the change. Add `"warnings": ["message"]` to include warnings in the successful MCP reply. Use `{"decision":"reject","message":"reason"}` to block the write and show the reason to the client. Use nonempty, printable strings and do not include private note text in messages

A timeout, nonzero exit, invalid response or stdout larger than 16 KiB blocks the write. The server discards stderr. The checker cannot return replacement text or change the result of `obsidian.delete`. This check applies only to writes through MCP

## Vault context

Set `checker.vault_context_properties = ["aliases", "tags", "up", "prev", "next"]` to opt in. Omit the list or leave it empty to keep the default request. The server adds `vault_context` only to Markdown writes and updates

| Key | Value |
| --- | --- |
| `property_names` | Names selected in the config, even when no note has those properties |
| `files` | Paths of all allowed files, including attachments and the proposed file |
| `notes` | Map from allowed Markdown paths to metadata, without the file under edit |
| `before`, `after` | Metadata for the current and proposed versions. For a new file, `before` is null |
| `links_before` | Link resolution for the old version of the note. Compare it with the top-level `links` for the new version, which has the same limits |

Each metadata entry has `frontmatter`, `properties` and `property_links`. Frontmatter status is `absent`, `valid`, `invalid` or `unreadable`. An unreadable note has no property data; treat it as unknown, not empty

`properties` contains only the keys in the config. Scalar values are strings, flat lists are arrays of strings, and other shapes are null. `property_links` maps property names to lists of wikilinks. Each link has `raw`, `target`, `anchor` and up to five `resolved` paths

If a rule needs vault context, reject the write when `vault_context` is absent or `property_names` lacks a required name. Check the selection rather than the property values in a note

The server reads only files in the current vault allowlist. It sends no bodies from other notes and does not collect inline tags. For each write, it builds a new snapshot from disk before it starts the script. This is not an atomic snapshot of edits from other applications

If the scan exceeds 64 MiB of note text, or the serialized context exceeds 4 MiB, the write fails rather than send a partial snapshot. Per note, the server also limits selected properties to 1 Mi characters and 200 property links. The checker timeout applies to the script, not to the scan

## Protect YAML frontmatter

Set `protect_frontmatter = true` in a writable vault to check Markdown files before `obsidian.write` and `obsidian.update`. This option is off by default and does not require a checker script

A frontmatter block starts at the top of the file and uses `---` delimiter lines. Its YAML must be empty or a map of properties without duplicate keys. The guard rejects a change from valid to invalid frontmatter. It warns if a write removes valid frontmatter or leaves or introduces invalid frontmatter where there was no valid block before

The guard does not require a header or particular properties. It does not enforce property types, preserve individual values or reformat the file

## Example

[examples/checker.py](../examples/checker.py) is a working checker. It warns about a one-line block formula and about underscore emphasis. Copy it, replace the rules, and point `checker.script` at your copy

## Example policy

This example rejects new occurrences of a placeholder line and warns when earlier occurrences remain:

```python
import json
import sys

request = json.load(sys.stdin)
marker = "INSERT SOURCE HERE"
before = (request["before"] or "").splitlines().count(marker)
after = request["after"].splitlines().count(marker)
if after > before:
    response = {"decision": "reject", "message": "Replace the new source placeholder"}
elif after:
    response = {"decision": "allow", "warnings": ["An earlier source placeholder remains"]}
else:
    response = {"decision": "allow"}
json.dump(response, sys.stdout)
```
