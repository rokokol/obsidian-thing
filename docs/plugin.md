# Obsidian plugin

The Obsidian Thing plugin uses the server index. It does not maintain its own index or edit notes. It requires Obsidian Desktop with Secret Storage support

## Install

Download `manifest.json`, `main.js`, and `styles.css` from the same [GitHub release](https://github.com/rokokol/obsidian-thing/releases). Put them in `.obsidian/plugins/obsidian-thing/` inside the vault and enable Obsidian Thing under Community plugins

To build from a checkout, use the Node version in [`plugin/.node-version`](../plugin/.node-version):

```sh
cd plugin
npm ci --ignore-scripts
npm run check
npm test
npm run build
```

The build puts the same install files in `plugin/dist/`

## Connect

Open the Obsidian Thing settings. Enter the full vault endpoint, including `/vaults/<vault_id>/mcp`. Use HTTPS outside a trusted local network. The plugin does not follow redirects

Paste the server token into **Store the vault token** and press Store. The plugin writes the value to Obsidian Secret Storage under `obsidian-thing-token` and keeps only that secret ID in its settings, so the token never reaches `data.json`. Press Test to list the tools the server offers for this vault

Give each vault its own endpoint and secret. Files open from the local vault at the exact path the server returns. If the local copy does not contain that path, the plugin cannot open it

## Search and explore

Open Obsidian Thing from the command palette. Text search uses `obsidian.query`. Metadata filters use [`obsidian.find`](semantic-search.md#metadata-search)

Open a note and request related files from the sidebar. Open the map from the related view. Set a neighbor limit or leave it blank to use the server default. Select a point or a list result to open the corresponding file. The server rank does not depend on the map coordinates

The plugin shows only features that the server lists. Without embeddings, text search and metadata filters still work if the vault has a local index. Use the status command to see the latest coverage snapshot and recorded costs

The server can call an embedding provider when you request text search, related files, or a map. It can index the source note for related results. The plugin does not contact the provider itself
