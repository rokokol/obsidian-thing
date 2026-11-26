# Markdown index benchmark

The corpus had 4,000 synthetic notes in 200 subdirectories. It contained 32,201,965 bytes of UTF-8 text with headings, paragraphs, and wikilinks. The host used an AMD Ryzen 7 5700X. The page cache was warm. The benchmark did not call an embedding provider

| Operation | Btrfs | tmpfs |
| --- | ---: | ---: |
| SHA-256 of note bytes already in memory | 15 ms | 15 ms |
| Vault enumeration and file metadata checks | 117 ms | 109 ms |
| Vault enumeration, file reads, UTF-8 checks, and SHA-256 | 228 ms | 214 ms |
| Initial local reconciliation | 13.74 s | 5.47 s |
| Local reconciliation with no changes | 364 ms | 341 ms |

Each result is the median of five runs, except the initial reconciliation, which ran once per file system. The metadata comparison uses the same vault enumeration, then calls `stat` once per file. The read comparison uses `Vault.read`. Both reconciliation runs use `SearchIndex.reconcile_local` with the normal FTS5 and link cache code

Keep the content hash for Markdown full scans. A metadata check alone can miss a change if the file size and modification time stay the same. The initial scan also parses notes and writes the local index; its duration does not measure SHA-256 alone. These warm-cache results do not estimate cold disk or network storage speeds
