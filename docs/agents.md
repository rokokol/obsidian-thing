# Vault instructions, skills and memory

An agent that works in a vault needs the vault's rules, its skills and what earlier agents learned about it. These files live in hidden paths, and [`obsidian.read`](integrations.md#obsidian-settings) never reaches a hidden path. The agent tools read them by name instead

## Configuration

```toml
[vaults.notes.agents]
instructions = ["AGENTS.md", "GEMINI.md", "CLAUDE.md"]
skills_dir = ".agents/skills"
memory_dir = ".agents/memory"
```

The table is optional, and the defaults above apply to every vault. Each path is relative to the vault root. Only the configured directory and the paths below it are reachable. A hidden name below it and a symbolic link are refused

## Instructions

`obsidian.agentsmd` returns the first file of `instructions` that exists and that the client may read. A file outside `allowed` is skipped, so a `GEMINI.md` never hides `AGENTS.md` from a client that may read only the latter. The tool returns a `VaultError` when no file of the list is readable

## Skills

`obsidian.skills_list` returns the name, description and `SKILL.md` path of every directory under `skills_dir`. It reads the YAML properties `name` and `description`. A directory with no readable `SKILL.md` is not a skill and does not appear. A hidden directory and a symbolic link are skipped

`obsidian.skill_read` reads `SKILL.md` of one skill. Pass `path` to read a file inside the same skill, such as `references/form.md`. The name is the directory name or the `name` property. A path outside the skill returns an error

## Memory

A memory note is a Markdown file under `memory_dir` with YAML frontmatter. The `name` and `description` properties label it in the index, and `metadata.type` is returned with it. `MEMORY.md` is the index and holds no note

| Tool | Result |
| --- | --- |
| `obsidian.memory_list` | Every note with its name, description, type, path and revision |
| `obsidian.memory_read` | One note by name without the extension. `MEMORY.md` reads the index |
| `obsidian.memory_write` | Create or replace a note. This tool requires a writable vault |
| `obsidian.memory_delete` | Hide a note and remove its line from `MEMORY.md`. This tool requires a writable vault |
| `obsidian.memory_index_write` | Replace `MEMORY.md` as a whole. This tool requires a writable vault |

`obsidian.memory_write` takes the revision from `obsidian.memory_read` and writes the note. It then writes the line of the note in `MEMORY.md` and returns `index_revision`. The index keeps the title it already has and takes the summary from the `description` property. A note without a description is refused

The server creates the memory directory together with the note. It saves the note before the index line, so a failed index write keeps the note and returns an error

`obsidian.memory_delete` takes the revision from `obsidian.memory_read` and renames the note to a hidden name beside the others, so the file stays restorable, then removes its line from `MEMORY.md`. A note with no line in the index is removed without touching the index

`obsidian.memory_index_write` takes the revision of `MEMORY.md` from `obsidian.memory_read`. It refuses the content when a line names no memory note, when a note has no line, or when a line appears twice, and it changes nothing in that case. Use it to reorder the index or to repair it after a note was removed with the file tools

The vault rules and the write checker do not apply to these files, because a memory note has no heading and no note text. The frontmatter check of `protect_frontmatter` still applies
