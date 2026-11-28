# Changelog

## Unreleased

### Changed

- Score neighbours with numpy over packed vectors and reuse the stored vector of a repeated query
- Read the first readable instruction file of `AGENTS.md`, `GEMINI.md` and `CLAUDE.md` with `obsidian.agentsmd`, and set the order per vault
- Rename each tool to the obsidian namespace, for example from `wiki.read` to `obsidian.read`
- Make Canvas, Bases and Media Gallery optional per vault. Media Gallery is off by default
- License the project under MIT

### Added

- List and read the skills of a vault under `.agents/skills` with `obsidian.skills_list` and `obsidian.skill_read`
- List, read and write the memory notes of a vault under `.agents/memory` with `obsidian.memory_list`, `obsidian.memory_read` and `obsidian.memory_write`, which updates the line of the note in `MEMORY.md`
- Create and replace UTF-8 files through `obsidian.write` in vaults that allow writes explicitly
- Replace a unique text fragment at a known revision through `obsidian.update`
- Move an unreferenced file to private trash with `obsidian.delete`
- Rename a file and update visible links with `obsidian.rename`
- Get a file revision and size without its bytes with `obsidian.stat`
- Allow trusted Python scripts to warn about or reject a proposed write before it reaches disk
- Optionally reject writes that make valid YAML frontmatter invalid
- Check new quotes, final periods, callout markers, inline code, heading formulas, link formulas, hard-wrapped paragraphs, broken links, bases and canvases through optional per-vault rules
- Reject new inline tags in Markdown with the optional `prevent_inline_tags` rule
- Inspect links, backlinks and attachment metadata with `obsidian.links_out`, `obsidian.links_in` and `obsidian.media`
- Include Canvas and static Base links in the graph, delete checks and rename edits
- Edit Canvas nodes and edges or Base filters, formulas and views in one batch
- Check Canvas and Base structure on every write when their integrations are enabled
- Set file icons in an offline Iconic vault
- Edit FSRS Vocabulary cards and read their memory state and review history
- Read explicit `.obsidian` paths with the vault token unless the owner disables this access
- Limit write warnings, with `all_warnings` to read every one
- Give custom write checkers optional access to selected vault properties and their link targets
- Ship an example checker in `examples/`
- Leave a heading without a body out of the index, and keep fragments shorter than `min_chunk_words` out of the vector index. The default is 24 words and `0` keeps every fragment
- Index Markdown and plain text for semantic search with Ollama or opt-in OpenRouter
- Record embedding calls and USD costs in a separate SQLite journal
- Report index coverage, the embedding queue, the last full scan, the model profile and recorded costs with `obsidian.status`
- Find indexed notes by tag, alias and YAML property with `obsidian.find`. The tool does not need embeddings
- Remove a memory note and its line in `MEMORY.md` with `obsidian.memory_delete`, and repair the whole index with `obsidian.memory_index_write`
- Draw a file and its nearest neighbors on two axes with `obsidian.map`. The tool keeps the vector rank and requires the plugin client header
- Name every tool and the configuration that offers it in one reference, with a page for every configuration key and a guide to deployment
- Publish a project site at `obsidian-thing.rokokol.art`: a bilingual overview, the tool reference, a short guide and a cover card for social previews

## [0.1.0] - 2026-11-19

### Added

- Read vault files and instructions through MCP Streamable HTTP
- Set an access token and allowed paths for each vault
- Run the server as a native Nix package or in Docker
