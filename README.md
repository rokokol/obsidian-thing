# Obsidian Thing

[![CI](https://github.com/rokokol/obsidian-thing/actions/workflows/build.yml/badge.svg)](https://github.com/rokokol/obsidian-thing/actions/workflows/build.yml) [![GHCR](https://img.shields.io/badge/container-GHCR-555)](https://github.com/rokokol/obsidian-thing/pkgs/container/obsidian-thing)

Obsidian Thing serves Obsidian vaults over MCP Streamable HTTP. Each vault has its own endpoint and bearer token. The server can read text files and vault instructions without a running Obsidian app

## Run locally

Copy [`config.example.toml`](config.example.toml) to `config.toml`. Set the vault root and the paths that each client may read. An allowed directory grants access to its subtree. Use `"."` to allow the whole vault, except for hidden paths and symbolic links

Set the environment variable named by `token_env` to a random secret. Use a different token for each vault that needs separate access. Start the server with:

```sh
uv run --locked obsidian-thing --config config.toml
```

Use the listen address printed by the server and the endpoint `/vaults/<vault_id>/mcp`. Send `Authorization: Bearer <token>` with each request. For remote access, put the server behind HTTPS and set `allowed_hosts` in the TOML file to the host names that the proxy forwards

Vaults allow reads by default. Set `writable = true` for a vault to enable `obsidian.write` and `obsidian.update` for any client with its token. Search is optional. See [TODO.md](TODO.md) for the remaining work

`obsidian.write` creates a UTF-8 file in an existing directory when you omit `expected_revision`. To replace a file, pass the revision from `obsidian.read`. Run one server process per writable vault. Revision checks do not protect against concurrent edits outside that process or unsaved Obsidian edits

`obsidian.update` replaces a non-empty `old` fragment with `new` at the revision given by `expected_revision`. The fragment must occur exactly once, including overlapping matches

Enable `obsidian.delete` with `trash_dir` in a writable vault. Use an existing private directory outside all vaults, owned by the server user. The trash and the file must share a mount. If the move fails, the file stays in the vault. The tool does not remove paths named directly in `allowed`, because the server needs them at startup

To restore a deletion, read `$trash_dir/<trash_id>/path` for the original vault-relative path. Move the sibling `file` back without overwriting an existing file. The server does not purge the trash

## Documentation

- [Python write checker](docs/checker.md): configure a script to warn about or reject proposed edits
- [Markdown rules for writes](docs/vault-rules.md): check quotes, periods, links and callout markers before a write
- [Example checker](examples/checker.py): a starting point for vault-specific rules
- [NixOS service](docs/nixos.md): configure the MCP server as a system service

## Run with Nix or Docker

Build from a Git checkout with secrets excluded from tracked files. Without Git, copy only `flake.nix`, `flake.lock`, `nixos-module.nix`, `pyproject.toml`, `VERSION`, and `src/` to a clean directory first. Nix can copy an entire local source tree to its store

```sh
nix run . -- --config /absolute/path/to/config.toml
nix build .#docker
docker load --input result
```

The image contains the same Nix package as the native command. It does not need a base image or a Nix daemon at runtime

For Docker, set the vault `root` in the config to `/vault`. Mount that path and the config as read-only volumes. Remove `:ro` from the vault mount only if the vault needs writes. Export the variable named by `token_env` before you start the container

For deletion, mount a common parent of the vault and trash directories as one volume. Set `root` and `trash_dir` to the two subdirectories inside the container

```sh
docker run --rm --read-only --user "$(id -u):$(id -g)" \
  --publish 127.0.0.1:8765:8765 \
  --env OBSIDIAN_MCP_NOTES_TOKEN \
  --volume /absolute/path/to/config.toml:/config.toml:ro \
  --volume /absolute/path/to/vault:/vault:ro \
  obsidian-thing:local --config /config.toml --host 0.0.0.0
```

## Semantic search

Enable embeddings per vault with the settings in [`config.example.toml`](config.example.toml). Create a private state directory outside the vault and mount it writable in Docker. Use a separate directory for each vault

Call `obsidian.index` to index one Markdown or plain-text file. Use `obsidian.query` for a text query or `obsidian.related` for neighbors of a note. `obsidian.related` indexes the source if needed, but searches only indexed files. The server skips deleted and outdated results. You can enable [automatic updates to the local index](docs/semantic-search.md#automatic-local-index-updates)

Use [Ollama, LM Studio, OpenRouter, or an OpenAI-compatible endpoint](docs/semantic-search.md#embedding-providers) for embeddings. Remote requests require explicit permission

Read `<state_dir>/spend.sqlite3` to inspect provider calls. The journal does not store input text, file paths, or keys. `reported` is a price returned by the provider. `estimated` uses the configured rate and the provider's token count. `unknown` means no usable price, and `local` means no API charge is tracked

```sh
sqlite3 /absolute/path/to/state/spend.sqlite3 'SELECT at, operation, provider, model, status, cost_kind, usd FROM calls ORDER BY id DESC LIMIT 20;'
```

## Verify

```sh
uv run --locked pytest -q
```

To test the loaded Docker image, run the same HTTP checks against a temporary container. The tests use synthetic vaults and do not call model providers

```sh
OBSIDIAN_MCP_TEST_IMAGE=obsidian-thing:local uv run --locked pytest -q tests/test_http.py
```
