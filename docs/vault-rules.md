# Markdown rules for writes

Enable any check in a table for a writable vault. Every key is independent and off by default

```toml
[vaults.notes.checks]
default_quotes = '""'
inner_quoter = "«»"
trailing_periods = true
prevent_inline_tags = true
callouts = true
broken_links = true
bases = true
canvas = true
```

The server compares the current file with the proposed text. A finding that the current version already has returns a warning, so an old mistake never blocks an edit. A new finding follows the rule's own level: rules report errors for facts that break a file and warnings for advice

Up to 10 warnings come back by default. The reply then names how many it left out. Pass `all_warnings = true` to `obsidian.write` or `obsidian.update` to read every warning

## default_quotes and inner_quoter

Both options take two characters: the opening and the closing quote. `default_quotes` is the pair for prose, and `inner_quoter` is the pair allowed inside it for a nested quotation. Leave both out to skip the rule

With `default_quotes = '""'` and `inner_quoter = "«»"`, plain quotes are the only form in prose and guillemets are valid only inside them: `"outer «inner» text"`. Every other mark, including `“ ” „`, is a finding. Configure `inner_quoter = false` to forbid nested quotation entirely

## trailing_periods

Applies to Markdown. Reject a new final period in a paragraph, list item or table cell. Keep periods inside a paragraph, ellipses and common abbreviations. The rule uses Markdown blocks, so a soft line break does not end a paragraph. The rule skips code, math blocks, headings and YAML frontmatter

## inline_code_equals

Applies to Markdown. Inline code must not start with an equals sign: Dataview reads such a span as its own query. A new finding rejects the write. Fenced code and math are not checked

## heading_math and unicode_math

Both apply to Markdown. `heading_math` rejects a Unicode math symbol in a heading, and `unicode_math` warns about one in prose. The rule reads only prose and headings, so code, inline code and math are not checked. A formula in TeX is allowed in prose and in a heading

A wikilink keeps Unicode math and is never reported by these rules. TeX inside a link breaks the target or the caption, so `link_math` rejects it

## link_math

Applies to Markdown. A formula in a wikilink target, anchor or caption is a finding: `[[Note|$x^2$]]`, `[[Note#$x$]]` and `![[img.png|$x$]]`. Write the symbol as Unicode inside a link. Code and inline code are not checked

## paragraph_lines

Applies to Markdown. A paragraph or a list item that spans several lines returns a warning, because the vault keeps one paragraph on one line. An intentional line break inside a paragraph, such as a poem, gives the same warning

## prevent_inline_tags

Applies to Markdown. Reject a new tag in the text, such as `#abcd` or `#inbox/to-read`. Put tags in the YAML `tags` property. Write a literal hash as `\#abcd` or use inline code

The rule skips code, formulas, link targets and labels, and YAML frontmatter. It does not change the note. An existing tag returns a warning if the edit leaves it in place

## callouts

Applies to Markdown. A callout marker `[!type]` in a blockquote must name a known type. Folded markers `[!type]-` and `[!type]+` are checked the same way. A marker outside a blockquote renders as text

Known types: `note`, `abstract`, `summary`, `tldr`, `info`, `todo`, `tip`, `hint`, `important`, `success`, `check`, `done`, `question`, `help`, `faq`, `warning`, `caution`, `attention`, `failure`, `fail`, `missing`, `danger`, `error`, `bug`, `example`, `quote`, `cite`

## broken_links

Applies to Markdown. Each wikilink and embed must resolve inside the vault and inside the allowed paths. Resolution follows Obsidian: a name without a dot also matches a Markdown note, and a folder in the link narrows the match. A heading or block anchor must exist in the target note

Use `obsidian.links` to see the resolution of every link in a note before a write

## bases

Applies to `.base` files. The file must be valid YAML with a mapping at the top. A view type must be `table`, `cards`, `list` or `map`. Every `formula.<name>` in `order` or `properties` must exist under `formulas`

## canvas

Applies to `.canvas` files. The file must be valid JSON. Node identifiers are unique across nodes and edges. A node type is `text`, `file`, `link` or `group`, and carries its required field. Every node has integer `x`, `y`, `width` and `height`. An edge names existing nodes, and its sides and ends use the allowed values. A literal `\\n` in node text is a warning
