# Semantic search

## Full-text search without a model

Set `enabled = false` in the vault's `embeddings` table. Keep `state_dir` to use `obsidian.index` and `obsidian.query` with only the local text index. The server does not load provider settings, start scheduled embedding jobs, or offer `obsidian.related`

Without `state_dir`, the server does not offer index tools. Set `indexing.auto = false` or omit it to avoid background scans. If you omit `enabled`, the `embeddings` table still enables vector search as before. If you omit the table, the vault has no search index

## Text fragments

The index splits Markdown at top-level headings, including nested heading levels. It splits long sections at blank lines and splits oversized paragraphs by size. Headings inside code fences do not start sections

Each fragment sent to the model includes its heading chain and the note's `tags` and `aliases`. The index reads scalar values and flat lists of scalars. It limits each property to 160 characters and the heading chain in the model input to 320 characters. It does not send other fields from a delimited frontmatter block. It indexes a table of contents written in the note as text. It does not generate a table of contents

Set `section_chunks = false` in the vault's `embeddings` table to split by size alone. This mode still includes `tags` and `aliases`. Plain text files always use size-based fragments and have no property or heading context

`obsidian.query` and `obsidian.related` return one result per file. Text matches include the selected fragment's `headings`, `line_start`, and `line_end`. Line numbers are inclusive and start at one. The ranges refer to the source file, not the model input

Run `obsidian.index` again for existing files after upgrade or after a mode change if that mode has no current index for the file. Search does not use entries from the previous format or the other mode. It can reuse cached vectors for unchanged model inputs

## Embedding providers

Set `provider`, `model`, and `dimensions` in the vault's `embeddings` table. The server rejects vectors of a different size. The cache separates vectors by endpoint and model profile. Set `profile_version` when you replace a model without a change to its name

| Provider | Endpoint | Options |
|---|---|---|
| `ollama` | `url` defaults to `http://127.0.0.1:11434`. The server adds `/api/embed` | The request disables truncation |
| `lm_studio` | `base_url` defaults to `http://127.0.0.1:1234/v1`. The server adds `/embeddings` | Set `request_dimensions = true` only if the endpoint accepts that field |
| `openai` | Set `base_url` to your OpenAI-compatible API base, for example `https://example.com/v1`. The server adds `/embeddings` | Set `request_dimensions = true` only if the endpoint accepts that field |
| `openrouter` | Fixed OpenRouter endpoint | Set `route` and `key_env`. The request disables fallback. `zdr` defaults to `true`. `data_collection` defaults to `"deny"` and also accepts `"allow"` |

Set `key_env` to the name of an environment variable if the endpoint requires a bearer token. HTTP is permitted only at `127.0.0.1` and `[::1]`. Other endpoints require HTTPS. The server does not follow redirects

To send text to a remote endpoint, set `allow_cloud = true` and specify `include_paths`. This permits the transfer of query text and fragments from notes in scope. `allow_cloud` defaults to `false`. When the server cannot use the provider, `query` returns text matches and sets `vector_unavailable`. OpenRouter route preferences do not grant this permission

## Attachment embeddings

The default `allowed_mime_types` contains `text/markdown` and `text/plain`. Append each attachment type that the server can send: `image/png`, `image/jpeg`, `application/pdf`, `audio/wav`, `audio/mpeg` (MP3), or `video/mp4`. Use `google/gemini-embedding-2` through OpenRouter on a Google Vertex route. Remote consent and embedding paths apply to attachments too

Files must match their formats. The server limits files and PDF pages to 5 MiB each, split PDF output to 10 MiB, images to 16 megapixels, and PDF documents to 16 pages. The server rejects encrypted PDF files. It sends one PDF page per request, without OCR or raster conversion

The server checks audio and video with `ffprobe` before transfer. Audio can last up to 180 seconds. MP4 can last up to 120 seconds and must use H.264, HEVC, AV1, or VP9. The file size limit still applies. For an installation outside Nix, put `ffprobe` with `fd` protocol support on the service's `PATH`

`obsidian.index` and the scheduled queue index enabled attachments. `obsidian.query` and `obsidian.related` can return an attachment. PDF matches include the best page as `page`, numbered from one. Attachment matches have no line fields. Full-text search stays text-only. `obsidian.media` does not return file bytes

## Embedding costs

Set `daily_budget_usd` in the vault's embedding profile to cap the local estimate for each UTC day. The budget applies to all remote calls for the vault. A vector cache hit does not use the budget. This setting does not change provider limits or billing

Set `usd_per_million_tokens` for text. Before a request, the server counts one estimated token per UTF-8 byte. This is a proxy, not the provider's token count. Set `usd_per_attachment_unit` by MIME type: per image, PDF page, second of audio, or source video frame. Use prices for the selected route. The source frame count can differ from the provider's billable count

Before transfer, the journal reserves the estimate. It keeps the estimate if the request fails without usage data. If the process stops before it records a response, the reservation stays until the UTC day ends. The journal uses the provider's cost when available. A missing estimate or an earlier call with unknown cost blocks remote requests while the budget is active

The journal is `spend.sqlite3` in the vault's state directory. It does not contain note or query text. The server marks costs as reported, estimated, unknown, or local. If a query cannot use embeddings, it falls back to full-text search

## Prometheus

Set the top-level `metrics_token_env` to the name of an environment variable that holds a separate bearer token. The server rejects a token that also grants access to a vault. Without this option, the server does not expose `/metrics`

Add a scrape job to Prometheus. Store the token in a file that only Prometheus can read. Use HTTPS if the connection leaves loopback

```yaml
scrape_configs:
  - job_name: obsidian-thing
    scheme: https
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials_file: /run/secrets/obsidian-thing-metrics
    static_configs:
      - targets: ["obsidian-thing.example.org:443"]
```

The exporter combines data from all vaults. Counter values cover the current process. The spend journal keeps the cost history. The queue gauge counts candidates in the index snapshot, before the worker checks their files. The scan timestamp stays zero until the watcher completes a full scan. It does not measure provider bill reconciliation

Use the Prometheus data source in Grafana. This query shows the share of remote calls with unknown cost. If there are no remote calls, the share has no value

```promql
sum(rate(obsidian_thing_embedding_cost_records_total{kind="unknown"}[1h]))
/
sum(rate(obsidian_thing_embedding_cost_records_total{kind!="local"}[1h]))
```

## Related files

`obsidian.related` uses the current vectors of the source file. It indexes that file if necessary, then searches the existing vector index. It does not reindex the vault

| Argument | Effect |
|---|---|
| `section_line` | Use a one-based line in a Markdown section with section chunks enabled. Select the heading or a line in its body |
| `include_paths` | Limit results to the specified vault-relative files or directories |
| `mime_types` | Limit results to the specified MIME types |
| `exclude_linked` | Exclude files that outgoing links in the source resolve to without path ambiguity |
| `min_score` | Override the vault profile's `related_min_score`. The default is zero. Valid values range from −1 to 1 |
| `limit` | Return no more than this number of files. The server does not add hits below the threshold |

The path and MIME filters apply only to results. They do not extend the vault or embedding scope. An empty list matches no files

Each file appears once, with its best score and location. The `sections` list contains matching text fragments or PDF pages in score order. Image, audio and video matches have no section locations. If you select a source section, the server can index missing chunks of the whole source file. Only vectors from the selected section contribute to the search

## Neighbourhood map

`obsidian.map` draws the source file and its nearest neighbours on two axes. The source can be a text file or an enabled attachment. It takes the same arguments as `obsidian.related`, including `limit`, and returns the same files in the same rank order. The `score` of a point is the cosine similarity that `obsidian.related` reports, so a short list is still ranked in the full vector space and never by the drawn position

The server reduces the file vectors with a principal component analysis. The mean vector of each file gives its point, so the position summarises a file rather than ranks it. `explained` gives the share of variance that each axis keeps. Coordinates fit a square from −1 to 1 and share one scale, so a drawn distance stays comparable to the distance it stands for. The same vectors always produce the same points, and the largest element of an axis fixes its sign, so a change of the linear algebra library cannot mirror a map. A file whose vectors cancel out sits at the origin

The tool needs an embedding profile. A vault with only full-text search does not offer it

## Index status

`obsidian.status` reports the local index without reading note bytes. It is available whenever the vault has a state directory, and it adds the embedding fields only when the vault has an embedding profile

| Field | Meaning |
|---|---|
| `index.mode` | `sections` or `size`, the split that the index holds |
| `index.files` | Visible files the index will consider: after the access, MIME, embedding scope and read-size rules |
| `index.excluded` | Files that `indexing.exclude` or a `.gitignore` rule removes |
| `index.indexed` | Files with an entry in the local full-text index; a note edited since the last scan still counts until a scan catches it |
| `index.unindexed` | Candidate files with no entry yet |
| `index.embedded` | Files with vectors for the current profile and split; a file with no vectors does not count |
| `index.pending` | Queued files that exist and pass the embedding scope check; the worker reads each one before it sends anything |
| `index.last_full_scan` | UTC time of the last full scan that ran to the end, or null |
| `embeddings.*` | Provider, model, dimensions, the first twelve characters of the profile, whether the route is remote, whether sending is allowed, the daily budget, the default threshold and the enabled MIME types |
| `spend.periods` | `24h`, `7d`, `30d` and `all`, each with `calls`, `reported`, `estimated`, `unknown_calls` and `local_calls` |
| `spend.daily` | The last 30 days that hold calls, with the same split for each day |

The counts show what the last scan found, filtered by the files that exist now: a deleted note leaves every count at once, and the next scan prunes its row. A note edited since the last scan still counts as indexed until a scan catches it. A `.gitignore` the server cannot parse raises an error instead of making every file look excluded

The server adds reported and estimated prices separately and returns them as decimal strings. A call with no usable price lands in `unknown_calls` and never inside a sum. A local route records `local_calls` and no price. A reservation that a stopped process left behind counts as an estimate until the UTC day ends, the same way the daily budget counts it. The status call reads the journal and the two index tables; it does not call the provider and does not change the index

## Index exclusions

Set `indexing.exclude` per vault to replace the default patterns `*.tmp`, `*.swp`, and `*~`. Patterns use `.gitignore` syntax relative to the vault root. An empty list disables these patterns. Hidden paths, including `.git`, and symlinks remain outside the index

Set `indexing.respect_gitignore = true` to also use `.gitignore` files from the vault root and its subdirectories. Nested files and `!` patterns apply. A negation cannot restore a file under an ignored directory. The server ignores symlinked `.gitignore` files. It does not use global Git rules or `.git/info/exclude`

These rules apply to explicit `obsidian.index` and `obsidian.related` calls, as well as search results. They do not change vault read access or link resolution. If a rule excludes a path after indexing, search hides its old entries without waiting for a new index operation

## Automatic local index updates

Set `indexing.auto = true` for a vault with a configured `embeddings` table. The server scans the vault at startup and at intervals set by `indexing.scan_seconds`. The default interval is 300 seconds, with a small random variation. These updates refresh FTS5 and the link cache without a call to the model

On Linux, file events trigger checks for specific paths. A directory change, lost events, or a replaced vault root triggers a full scan. Other systems and failed file watches use periodic scans. Local updates do not call the model. Each full scan checks the bytes of enabled attachments even if their metadata did not change. Path-specific checks read the bytes only when metadata changes or no snapshot exists. `obsidian.index` and `obsidian.related` can still request vectors immediately

## Scheduled embedding updates

Set `embeddings.background = true` to allow scheduled requests to the model. Set `embeddings.schedule` to a five-field cron expression in UTC. The default is `0 3 * * *`. A missed run waits until the next scheduled time

Each scheduled run checks the vault before it processes the queue. SQLite records the latest local revision and the completed vector revision. A difference marks pending work. The server skips deleted, outdated, and excluded files. It reuses cached vectors for unchanged model inputs. Provider errors leave unfinished work for the next run

## Full-text and hybrid search

`obsidian.index` records local FTS5 entries before it requests vectors. If the provider fails, the call reports the error, but the text entries remain available. Run the index tool on previously indexed files to add their text entries. Both result lists enforce vault access and index exclusions. Only vector results must also fall inside the embedding scope

`obsidian.query` treats the input as ordinary text, not FTS5 syntax. It searches for any of the query words. Underscores stay inside tokens, so a term such as `section_chunks` stays intact. The tokenizer does not reduce Russian words to their base forms

The server ranks text results with BM25 and vector results with cosine similarity. It combines their ranks with reciprocal rank fusion. The returned `score` is a rank score, not a cosine similarity. The location comes from the result with the better rank. Text wins a tie

If the embedding provider fails, `obsidian.query` returns text matches and sets `vector_unavailable` to `true`. `obsidian.related` still uses only vectors, and its `score` remains a cosine similarity

The private embedding `state_dir` now also contains indexed note text in `search.sqlite3`. Keep that directory private. Revoked paths no longer appear in results, but the search database can still contain their text
