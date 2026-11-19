# Changelog

## Unreleased

### Added

- Create and replace UTF-8 files through `wiki.write` in vaults that allow writes explicitly
- Replace a unique text fragment at a known revision through `wiki.update`
- Move a file at a known revision to an optional private trash directory through `wiki.delete`
- Allow trusted Python scripts to warn about or reject a proposed write before it reaches disk
- Index Markdown and plain text for semantic search with Ollama or opt-in OpenRouter
- Record embedding calls and USD costs in a separate SQLite journal

## [0.1.0] - 2026-11-19

### Added

- Read vault files and instructions through MCP Streamable HTTP
- Set an access token and allowed paths for each vault
- Run the server as a native Nix package or in Docker
