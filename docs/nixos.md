# NixOS service

Add the Obsidian Thing flake as an input. Import its `nixosModules.default` output in your host configuration

```nix
{
  imports = [ inputs.obsidian-thing.nixosModules.default ];

  obsidian-thing = {
    enable = true;
    environmentFile = "/run/secrets/obsidian-thing.env";
    settings = {
      vaults.notes = {
        root = "/srv/notes";
        allowed = [ "." ];
        token_env = "OBSIDIAN_THING_VAULT_TOKEN";
        indexing.auto = true;
      };
      metrics_token_env = "OBSIDIAN_THING_METRICS_TOKEN";
    };
  };
}
```

The module converts `settings` to the same TOML format that the command-line server uses. Put only non-secret configuration here: the Nix store can expose it. Supply both tokens in the runtime environment file. Use a separate token for [Prometheus](semantic-search.md#prometheus), or omit `metrics_token_env` to disable metrics

The default service account is `obsidian-thing`. Grant that account access to the vault, or set `user` and `group` to an existing unprivileged account. This does not change the vault's owner or permissions. For a writable vault, set `writable` and give the account write access. The unit does not grant write access to read-only vaults

The module creates private state directories under `/var/lib/obsidian-thing/` for automatic indexing or embeddings. With automatic indexing and no embedding provider, the server uses full-text search only. If you set `embeddings.state_dir`, create and secure that directory separately

The service listens on loopback by default. Keep it on loopback when a reverse proxy serves MCP to other hosts. If Prometheus runs on the same host, scrape `/metrics` on the loopback address. A proxy that serves MCP need not expose `/metrics`
