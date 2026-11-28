# Deployment

The server is one process that serves one endpoint for each vault in its configuration. It needs Python 3.11 or newer, and it runs on Linux, macOS and the BSDs. The file watcher is Linux-only. Other systems reconcile the index by periodic scans

`ffprobe` with the `fd` protocol belongs on the `PATH` when a vault enables audio or video attachments. The Nix package carries it

## From the flake

```sh
nix run . -- --config /absolute/path/to/config.toml
```

The package and the Docker image come from the same derivation:

```sh
nix build .#default
./result/bin/obsidian-thing --config /absolute/path/to/config.toml

nix build .#docker --out-link result-docker
docker load --input result-docker
```

Nix copies the source tree into the store, so build from a Git checkout that tracks no secret. Without Git, copy `flake.nix`, `flake.lock`, `nixos-module.nix`, `pyproject.toml`, `VERSION` and `src/` to a clean directory first

## From a checkout

```sh
uv sync --locked
uv run --locked obsidian-thing --config /absolute/path/to/config.toml
```

`--host` defaults to `127.0.0.1` and `--port` to `8765`. The server prints both at start

## The endpoint

Each vault answers at `/vaults/<vault_id>/mcp` and accepts one bearer token. This read of one note shows the whole handshake, and the tools of a vault appear in its own list

```sh
export OBSIDIAN_THING_NOTES_TOKEN=<token>

curl -sS http://127.0.0.1:8765/vaults/notes/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "Authorization: Bearer $OBSIDIAN_THING_NOTES_TOKEN" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"1"}}}'

curl -sS http://127.0.0.1:8765/vaults/notes/mcp \
  -H 'Content-Type: application/json' \
  -H 'Accept: application/json, text/event-stream' \
  -H "Authorization: Bearer $OBSIDIAN_THING_NOTES_TOKEN" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"obsidian.read","arguments":{"path":"Notes/Hello.md"}}}'
```

The response carries `path`, `content` and `revision`, and the revision is the SHA-256 of the file bytes. A request without the token, or with another token, gets `401`. The server holds no session, so each call stands alone and needs no handshake of its own

## Tokens

```sh
openssl rand -hex 32
```

The token accepts ASCII letters, digits and `._~+/=-`. Use a different token for each vault that needs separate access, and another one for `/metrics`. Store them in an environment file that only the service user can read, and name that file as `EnvironmentFile` in a unit or as `--env-file` in Docker

One server process serves every vault in its configuration. A token grants access to its own vault only

## One process for a writable vault

The server serializes its own mutations to a path, and a revision check catches every change it can see. It cannot see an unsaved buffer in Obsidian or a concurrent writer outside the process. Run one server process for each vault that agents write through. Two processes on one vault turn a revision check into a guess

The file watcher reconciles the index from file events and periodic full scans. A remote file system that does not deliver inotify events falls back to the scans, which costs a full walk at each interval

## Behind a reverse proxy

The client sends `Authorization: Bearer <token>`. The proxy forwards that header unchanged and must not cache the response. The public host name belongs in `allowed_hosts`, because the transport rejects another `Host`

```nginx
location /vaults/ {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Host $host;
    proxy_set_header Authorization $http_authorization;
    proxy_buffering off;
    proxy_read_timeout 300s;
}
```

A long call, such as indexing a large note or a PDF, waits for the provider. Raise `proxy_read_timeout` above the provider's own timeout, or the proxy closes a request that the server is still working on

A browser client needs its origin in `allowed_origins`. A command-line agent or the Obsidian plugin sends no `Origin` and needs nothing extra

## Docker

The image holds the native package and needs no Nix daemon at runtime

```sh
docker run --rm --read-only --user "$(id -u):$(id -g)" \
  --publish 127.0.0.1:8765:8765 \
  --env OBSIDIAN_THING_NOTES_TOKEN \
  --volume /absolute/path/to/config.toml:/config.toml:ro \
  --volume /absolute/path/to/vault:/vault:ro \
  obsidian-thing:local --config /config.toml --host 0.0.0.0
```

Set the vault `root` in the configuration to `/vault`, and mount that path. Remove `:ro` from the vault mount only when the vault needs writes, and add the state and trash mounts beside it. Inside the container, `root`, `state_dir` and `trash_dir` name paths under the mounts

The image listens on loopback by default, so a container publishes it with `--host 0.0.0.0`

## Deletion

Set `trash_dir` to a private absolute directory for a writable vault. The trash and the file must share a mount, and the directory must not overlap a vault

`obsidian.delete` moves the file to `<trash_dir>/<trash_id>/file` and writes the original vault-relative path to `<trash_dir>/<trash_id>/path`. To restore a file, read that path, then move the sibling `file` back without overwriting an existing file. The server never purges the trash, so the owner decides when to empty it

In Docker, mount a common parent of the vault and the trash as one volume, so the move stays inside one file system

## The state directory

A searching vault needs a private state directory, owned by the server user, with no permission for group or other. It holds two databases:

| File | Contents |
| --- | --- |
| `search.sqlite3` | The full-text index and the vectors, and the text of the indexed notes |
| `spend.sqlite3` | The journal of provider calls, their tokens, latency and cost |

Both are private, and the first one repeats the vault's text. Keep the directory out of every vault and out of every backup that leaves the machine, or treat it with the same care as the vault

The index is rebuildable. Delete both files and the next scan rebuilds them without a provider call for text, and with the provider for vectors. The spend journal is not rebuildable, so keep it if you need the cost history

## Metrics

Set the top-level `metrics_token_env` to the name of an environment variable that holds a separate bearer token. The server refuses a token that also opens a vault. Without that key the `/metrics` route does not exist

Prometheus scrapes `/metrics` over loopback, or through the proxy that also passes `/vaults/`. See [Prometheus](semantic-search.md#prometheus) for a scrape job and a query

## NixOS

The flake exports `nixosModules.default`, which writes the configuration, creates the private state directories, and runs the service as an unprivileged user. See [NixOS service](nixos.md)

## Verify the deployment

Run the two `curl` calls above against the deployed address. Then check the four answers that a wrong deployment gives, each one already shown here:

| Check | Answer |
| --- | --- |
| The token of one vault on the address of another | `401` |
| A vault ID that the configuration does not name | `404` |
| A `Host` header outside `allowed_hosts` | `421`, with `Invalid Host header` |
| `obsidian.read` of a file you edited by hand | The new `revision` |
