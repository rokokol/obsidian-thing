<div align="center">

# Obsidian Thing

**Your Obsidian vault behind an MCP endpoint: one address and one token for each vault, reads without Obsidian running, writes that check the revision and your own rules**

[![CI](https://github.com/rokokol/obsidian-thing/actions/workflows/build.yml/badge.svg)](https://github.com/rokokol/obsidian-thing/actions/workflows/build.yml)
[![GHCR](https://img.shields.io/badge/container-GHCR-555)](https://github.com/rokokol/obsidian-thing/pkgs/container/obsidian-thing)
[![license](https://img.shields.io/badge/license-MIT-3DA639)](LICENSE)
[![workarounds](https://img.shields.io/badge/docs-workarounds-555)](WORKAROUNDS.md)

</div>

An agent that works in a vault needs to read notes, follow links, find related material and sometimes write. Obsidian Thing serves that over MCP Streamable HTTP from a directory on disk. It does not need the Obsidian app, a plugin or a sync service. Each vault has its own endpoint and its own bearer token, and an allowlist decides which paths that token reaches

Writing is off by default. A writable vault gets atomic replacement at a revision and optional rules for quotes, periods, links, callouts, Canvas and Base files. Anything those rules do not cover is a linter of your own: a Python script that sees every proposed write and allows it, warns about it or refuses it. A file that lands in the trash comes back by hand

Search is optional and stays local. Text search needs no model. Metadata search filters notes by tag, alias, property or file name. Embeddings add semantic search, related notes and a two-axis neighbourhood map, from a local server or a remote provider that you permit explicitly

## Quick start

Copy [`config.example.toml`](config.example.toml) to `config.toml` and name one vault:

```toml
[vaults.notes]
root = "/srv/notes"
allowed = ["."]
token_env = "OBSIDIAN_THING_NOTES_TOKEN"
```

Export the token and start the server:

```sh
export OBSIDIAN_THING_NOTES_TOKEN=$(openssl rand -hex 32)
nix run . -- --config config.toml
```

Point the client at `http://127.0.0.1:8765/vaults/notes/mcp` and send `Authorization: Bearer $OBSIDIAN_THING_NOTES_TOKEN`. For a checkout without Nix, run `uv run --locked obsidian-thing --config config.toml`. The [deployment guide](docs/deployment.md) covers Docker, a reverse proxy, tokens, the state directory and the trash

## What the agent gets

- **Reading.** Files with their revision, metadata without the bytes, outgoing links, backlinks and attachment metadata. Canvas and Base files count as documents, and their links join the graph
- **Writing.** Create, replace, edit one fragment, rename with link updates, and move a file to a private trash. Every change carries an expected revision, and a rename refuses rather than leave a link broken
- **Vault instructions.** The vault's own rules, its skills and its memory: `AGENTS.md`, `.agents/skills` and `.agents/memory`, read by name instead of through the path allowlist
- **Search.** Full-text ranking without a model, metadata filters, vectors, related notes and a neighbourhood map. An index status call reports coverage, the embedding queue and recorded provider costs
- **Vault integrations.** Canvas and Base structural edits, Iconic file icons, FSRS Vocabulary cards and offline Excalidraw export, each one a switch per vault
- **One Obsidian plugin.** Search, related notes, the map and the index status inside the app, from the server's own index

The [tool reference](docs/tools.md) lists every tool with its arguments and the switch that offers it

## Documentation

- [Tools](docs/tools.md): every tool, its arguments and the configuration that offers it
- [Configuration](docs/config.md): every key of the TOML file and what the server refuses at start
- [Deployment](docs/deployment.md): Nix, Docker, a reverse proxy, tokens and the trash
- [NixOS service](docs/nixos.md): the module, the service user and the state directories
- [Vault instructions, skills and memory](docs/agents.md): what an agent reads before it edits
- [Semantic search](docs/semantic-search.md): the index, providers, attachments, costs and Prometheus
- [Markdown rules](docs/vault-rules.md): quotes, periods, links, callouts and hard-wrapped paragraphs
- [Write checker](docs/checker.md): a trusted script that warns about or rejects a proposed edit
- [Structured edits](docs/structured-edits.md): Canvas and Base changes as a batch
- [Links and attachments](docs/graph.md): link resolution, delete and rename
- [Vault integrations](docs/integrations.md): the per-vault switches
- [Obsidian plugin](docs/plugin.md): install, connect and use the sidebar and the map
- [Index benchmark](docs/index-benchmark.md): measured scan and hash times on a 4,000-note vault
- [Workarounds](WORKAROUNDS.md): why a few things are the way they are
- [Remaining work](TODO.md): the open items, in Russian

## Verify

```sh
uv run --locked pytest -q
```

The suite uses synthetic vaults and calls no model provider. To test the loaded Docker image, run the same HTTP checks against a temporary container:

```sh
nix build .#docker --out-link result-docker
docker load --input result-docker
OBSIDIAN_MCP_TEST_IMAGE=obsidian-thing:local uv run --locked pytest -q tests/test_http.py
```
