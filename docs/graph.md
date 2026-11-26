# Links and attachments

Pass a vault-relative `path` to each tool. The tools use the selected vault and its allowed paths. They do not require Obsidian, an embedding provider or a separate index

| Tool | Result |
| --- | --- |
| `obsidian.links_out` | `links`: each link in a Markdown note, Canvas or Base, with its target and status |
| `obsidian.links_in` | `links`: occurrences that point to the requested file. `skipped`: visible documents that the server could not inspect |
| `obsidian.media` | `media`: one entry per local attachment, with `path`, `mime_type`, `size_bytes` and the links to it |

Each link has `source`, `raw`, `target`, `anchor`, `embed`, `syntax`, `origin`, `status` and `resolved`. The `resolved` list contains vault paths, not URLs. `media` includes non-Markdown files such as images, audio and PDFs. It guesses MIME types from file names and never returns file bytes

| Status | Meaning |
| --- | --- |
| `resolved` | One file matches. Its Markdown anchor exists if the link has one |
| `unresolved` | No visible file matches |
| `ambiguous` | More than one file matches. No candidate gets a backlink |
| `unresolved_anchor` | One file matches, but its anchor does not exist |
| `ambiguous_anchor` | One file matches, but its anchor occurs more than once |
| `external` | The link points outside the vault. The server does not fetch it |

The graph reads Markdown links, wikilinks, embeds, and whole link values in YAML properties. A property link has `origin: property` and a `property` key with its dotted name. Text that contains a link does not count as a property link

Code does not count. An exception applies to entries in `media-gallery`, `img-gallery` and `img-gal` fences. A `path:` entry selects media in a folder relative to the vault root. A plain folder and `folder/**` include subfolders. `folder/*` does not. The selectors `.`, `/`, `*` and `**` select all visible media

A folder entry produces one link per visible media file, with `syntax: gallery_path`. If no files match, the entry returns as `unresolved`. The graph does not apply gallery display options, such as limits, extension filters or exclusions. It does not expand flexible wildcard patterns

A path that starts with `./` or `../` is relative to the source note. A path that starts with `/` starts at the vault root. Other paths match by name or path suffix. Paths match without regard to case and after Unicode normalization. A note alias does not identify a file. Markdown links decode URL escapes. Wikilinks keep them literal

The graph reads files on demand and caches parsed links for each note revision. It resolves them against the current visible paths on each call. Backlinks scan visible Markdown, Canvas and Base files. `media` and outgoing links read the requested document and targets whose anchors need a check

Canvas file and group background paths match exact vault paths. The graph also reads Markdown in text cards and records URL cards as external. Canvas edges between cards are not links to vault files

Bases contribute literal file references in `link()`, `file()`, `image()` and `.hasLink()`. The graph reads these calls in formulas, summaries and filters, including view filters. It checks view names in links such as `![[Table.base#Overview]]`. It does not evaluate formulas, infer paths from other strings or treat filter results as links

## Delete or rename files

Both operations require a writable vault and `expected_revision`. Get the revision of a text file from `obsidian.read`. Use `obsidian.stat` for any file. It returns `path`, `revision` and `size_bytes`, not the file bytes

- `obsidian.delete(path, expected_revision)` refuses if links from other visible documents would lose their file target. A self-link is removed with the note. A Media Gallery folder selector does not link to an individual member. The tool needs a configured trash directory
- `obsidian.rename(path, new_name, expected_revision)` keeps the directory and file extension. It does not overwrite an existing file. It updates links to the old name and returns the new revision and a list of changed documents. It refuses if a link cannot be edited safely or if the new name would change other link targets or anchors

Both operations refuse if they cannot inspect a visible Markdown, Canvas or Base file. They do not read or change paths outside the allowlist. You cannot delete or rename a path listed directly in the allowlist

Rename checks the proposed edits against configured write rules before it publishes them. It stages the edits and tries to restore files if a write fails. A crash can interrupt a rename. One server process serializes MCP mutations, but it cannot lock out Obsidian or other writers
