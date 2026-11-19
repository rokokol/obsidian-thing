# Check writes before they reach disk

Set `checker.script` for a writable vault in [the example config](../config.example.toml). If you omit the checker table, the server does not run a script. In Docker, mount the script or its directory read-only and use its absolute path inside the container

The server runs the script with its Python interpreter, in isolated mode, with an empty environment. Keep the script outside all vaults, trash and embedding state. Use only trusted code: the process has the server user's permissions and is not a sandbox

The script receives one UTF-8 JSON object on stdin. `operation` is `write` or `update`, and `path` is relative to the vault. `before` is null for a new file. `after` is the complete proposed text, even for a fragment edit

```json
{"version":1,"operation":"update","path":"Notes/Example.md","before":"Old text\n","after":"New text\n"}
```

Exit with status zero and write one UTF-8 JSON response to stdout. Use `{"decision":"allow"}` to accept the change. Add `"warnings": ["message"]` to include warnings in the successful MCP reply. Use `{"decision":"reject","message":"reason"}` to block the write and show the reason to the client. Use nonempty, printable strings and do not include private note text in messages

A timeout, nonzero exit, invalid response or stdout larger than 16 KiB blocks the write. The server discards stderr. The checker cannot return replacement text or change the result of `wiki.delete`. This check applies only to writes through MCP

## Protect YAML frontmatter

Set `protect_frontmatter = true` in a writable vault to check Markdown files before `wiki.write` and `wiki.update`. This option is off by default and does not require a checker script

A frontmatter block starts at the top of the file and uses `---` delimiter lines. Its YAML must be empty or a map of properties without duplicate keys. The guard rejects a change from valid to invalid frontmatter. It warns if a write removes valid frontmatter or leaves or introduces invalid frontmatter where there was no valid block before

The guard does not require a header or particular properties. It does not enforce property types, preserve individual values or reformat the file

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
