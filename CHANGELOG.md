# Changelog

## Unreleased

### Changed

- Rename every tool from `wiki.*` to `obsidian.*`

### Added

- Create and replace UTF-8 files through `obsidian.write` in vaults that allow writes explicitly
- Replace a unique text fragment at a known revision through `obsidian.update`
- Move a file at a known revision to an optional private trash directory through `obsidian.delete`
- Allow trusted Python scripts to warn about or reject a proposed write before it reaches disk
- Optionally reject writes that make valid YAML frontmatter invalid
- Check new quotes, final periods, callout markers, inline code, heading formulas, link formulas, hard-wrapped paragraphs, broken links, bases and canvases through optional per-vault rules
- Reject new inline tags in Markdown with the optional `prevent_inline_tags` rule
- Resolve every wikilink in a note with `obsidian.links`
- Limit write warnings, with `all_warnings` to read every one
- Give custom write checkers optional access to selected vault properties and their link targets
- Ship an example checker in `examples/`
- Index Markdown and plain text for semantic search with Ollama or opt-in OpenRouter
- Record embedding calls and USD costs in a separate SQLite journal

## [0.1.0] - 2026-11-19

### Added

- Read vault files and instructions through MCP Streamable HTTP
- Set an access token and allowed paths for each vault
- Run the server as a native Nix package or in Docker
