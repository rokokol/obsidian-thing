# Semantic search

The index splits Markdown at top-level headings, including nested heading levels. It splits long sections at blank lines and splits oversized paragraphs by size. Headings inside code fences do not start sections

Each fragment sent to the model includes its heading chain and the note's `tags` and `aliases`. The index reads scalar values and flat lists of scalars. It limits each property to 160 characters and the heading chain in the model input to 320 characters. It does not send other fields from a delimited frontmatter block. It indexes a table of contents written in the note as text. It does not generate a table of contents

Set `section_chunks = false` in the vault's `embeddings` table to split by size alone. This mode still includes `tags` and `aliases`. Plain text files always use size-based fragments and have no property or heading context

`obsidian.query` and `obsidian.related` return one result per file with the selected fragment's `headings`, `line_start`, and `line_end`. Line numbers are inclusive and start at one. The ranges refer to the source file, not the model input

Run `obsidian.index` again for existing files after upgrade or after a mode change if that mode has no current index for the file. Search does not use entries from the previous format or the other mode. It can reuse cached vectors for unchanged model inputs

## Index exclusions

Set `indexing.exclude` per vault to replace the default patterns `*.tmp`, `*.swp`, and `*~`. Patterns use `.gitignore` syntax relative to the vault root. An empty list disables these patterns. Hidden paths, including `.git`, and symlinks remain outside the index

Set `indexing.respect_gitignore = true` to also use `.gitignore` files from the vault root and its subdirectories. Nested files and `!` patterns apply. A negation cannot restore a file under an ignored directory. The server ignores symlinked `.gitignore` files. It does not use global Git rules or `.git/info/exclude`

These rules apply to explicit `obsidian.index` and `obsidian.related` calls, as well as search results. They do not change vault read access or link resolution. If a rule excludes a path after indexing, search hides its old entries without waiting for a new index operation

## Full-text and hybrid search

`obsidian.index` records local FTS5 entries before it requests vectors. If the provider fails, the call reports the error, but the text entries remain available. Run the index tool on previously indexed files to add their text entries. Both result lists enforce vault access and index exclusions. Only vector results must also fall inside the embedding scope

`obsidian.query` treats the input as ordinary text, not FTS5 syntax. It searches for any of the query words. Underscores stay inside tokens, so a term such as `section_chunks` stays intact. The tokenizer does not reduce Russian words to their base forms

The server ranks text results with BM25 and vector results with cosine similarity. It combines their ranks with reciprocal rank fusion. The returned `score` is a rank score, not a cosine similarity. The location comes from the result with the better rank. Text wins a tie

If the embedding provider fails, `obsidian.query` returns text matches and sets `vector_unavailable` to `true`. `obsidian.related` still uses only vectors, and its `score` remains a cosine similarity

The private embedding `state_dir` now also contains indexed note text in `search.sqlite3`. Keep that directory private. Revoked paths no longer appear in results, but the search database can still contain their text
