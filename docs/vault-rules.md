# Markdown rules for writes

Enable any check in a table for a writable vault. Every key is independent and off by default

```toml
[vaults.notes.checks]
quotes = true
callouts = true
broken_links = true
bases = true
canvas = true
```

The server compares the current file with the proposed text. A finding that the current version already has returns a warning, so an old mistake never blocks an edit. A new finding follows the rule's own level: rules report errors for facts that break a file and warnings for advice

Up to 10 warnings come back by default. The reply then names how many it left out. Pass `all_warnings = true` to `wiki.write` or `wiki.update` to read every warning

## quotes

Applies to Markdown. Plain `"` quotes are the only form in prose. Guillemets `«…»` are valid only inside a pair of plain quotes, as a nested quotation: `"outer «inner» text"`. Typographic quotes `“ ” „` are always findings

## callouts

Applies to Markdown. A callout marker `[!type]` in a blockquote must name a known type. Folded markers `[!type]-` and `[!type]+` are checked the same way. A marker outside a blockquote renders as text

Known types: `note`, `abstract`, `summary`, `tldr`, `info`, `todo`, `tip`, `hint`, `important`, `success`, `check`, `done`, `question`, `help`, `faq`, `warning`, `caution`, `attention`, `failure`, `fail`, `missing`, `danger`, `error`, `bug`, `example`, `quote`, `cite`

## broken_links

Applies to Markdown. Each wikilink and embed must resolve inside the vault and inside the allowed paths. Resolution follows Obsidian: a name without a dot also matches a Markdown note, and a folder in the link narrows the match. A heading or block anchor must exist in the target note

Use `wiki.links` to see the resolution of every link in a note before a write

## bases

Applies to `.base` files. The file must be valid YAML with a mapping at the top. A view type must be `table`, `cards`, `list` or `map`. Every `formula.<name>` in `order` or `properties` must exist under `formulas`

## canvas

Applies to `.canvas` files. The file must be valid JSON. Node identifiers are unique across nodes and edges. A node type is `text`, `file`, `link` or `group`, and carries its required field. Every node has integer `x`, `y`, `width` and `height`. An edge names existing nodes, and its sides and ends use the allowed values. A literal `\\n` in node text is a warning
