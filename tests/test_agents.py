from hashlib import sha256

import pytest

from obsidian_thing.agents import (DEFAULT_INSTRUCTIONS, list_memory, list_skills, read_instructions,
                                   read_memory, read_skill, write_memory)
from obsidian_thing.vault import Vault, VaultError

SKILLS = '.agents/skills'
MEMORY = '.agents/memory'


def make_vault(tmp_path, *, allowed=None, **kwargs):
    root = tmp_path / 'vault'
    root.mkdir()
    kwargs.setdefault('agent_dirs', [SKILLS, MEMORY])
    return Vault(root, allowed or ['.'], **kwargs)


def write_skill(vault, name, description, *, declared=None, body='# Skill\n'):
    directory = vault.root / SKILLS / name
    directory.mkdir(parents=True)
    content = f'---\nname: {declared or name}\ndescription: {description}\n---\n{body}'
    (directory / 'SKILL.md').write_text(content)
    return content


def write_memory_note(vault, name, *, description='A fact', kind='reference', body='Body\n'):
    directory = vault.root / MEMORY
    directory.mkdir(parents=True, exist_ok=True)
    content = (f'---\nname: {name}\ndescription: {description}\nmetadata:\n  type: {kind}\n---\n{body}')
    path = directory / f'{name}.md'
    path.write_text(content)
    return content, path


def test_instructions_use_the_first_readable_file(tmp_path):
    vault = make_vault(tmp_path)
    (vault.root / 'GEMINI.md').write_text('gemini rules')
    assert read_instructions(vault, DEFAULT_INSTRUCTIONS)['content'] == 'gemini rules'
    (vault.root / 'AGENTS.md').write_text('agent rules')
    assert read_instructions(vault, DEFAULT_INSTRUCTIONS)['content'] == 'agent rules'
    assert read_instructions(vault, ['CLAUDE.md', 'GEMINI.md'])['path'] == 'GEMINI.md'


def test_instructions_skip_a_file_the_client_may_not_read(tmp_path):
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'GEMINI.md').write_text('gemini rules')
    (root / 'AGENTS.md').write_text('agent rules')
    vault = Vault(root, ['AGENTS.md'], agent_dirs=[SKILLS, MEMORY])
    assert read_instructions(vault, ['GEMINI.md', 'AGENTS.md'])['content'] == 'agent rules'
    with pytest.raises(VaultError, match='GEMINI.md'):
        read_instructions(vault, ['GEMINI.md'])


def test_skills_list_reads_frontmatter_and_skips_unloadable_entries(tmp_path):
    vault = make_vault(tmp_path)
    write_skill(vault, 'style', 'Form of a note')
    write_skill(vault, 'plain', 'No references')
    (vault.root / SKILLS / 'broken').mkdir()
    hidden = vault.root / SKILLS / '.hidden'
    hidden.mkdir()
    (hidden / 'SKILL.md').write_text('---\nname: hidden\ndescription: Hidden\n---\n')
    (vault.root / SKILLS / 'link').symlink_to(vault.root / SKILLS / 'style', target_is_directory=True)
    assert list_skills(vault, SKILLS) == {'skills': [
        {'name': 'plain', 'description': 'No references', 'path': f'{SKILLS}/plain/SKILL.md'},
        {'name': 'style', 'description': 'Form of a note', 'path': f'{SKILLS}/style/SKILL.md'},
    ]}


def test_skills_list_of_a_missing_directory_is_empty(tmp_path):
    assert list_skills(make_vault(tmp_path), SKILLS) == {'skills': []}


def test_skill_read_returns_the_skill_and_its_files(tmp_path):
    vault = make_vault(tmp_path)
    write_skill(vault, 'other', 'Another skill')
    content = write_skill(vault, 'style', 'Form of a note')
    references = vault.root / SKILLS / 'style' / 'references'
    references.mkdir()
    (references / 'form.md').write_text('Form\n')
    skill = read_skill(vault, SKILLS, 'style')
    assert skill == {'path': f'{SKILLS}/style/SKILL.md', 'content': content,
                     'revision': sha256(content.encode()).hexdigest()}
    assert read_skill(vault, SKILLS, 'style', 'references/form.md')['content'] == 'Form\n'


def test_skill_read_resolves_a_declared_name(tmp_path):
    vault = make_vault(tmp_path)
    write_skill(vault, 'directory-name', 'Named differently', declared='declared-name')
    assert read_skill(vault, SKILLS, 'declared-name')['path'] == f'{SKILLS}/directory-name/SKILL.md'


@pytest.mark.parametrize('name,path', [
    ('missing', 'SKILL.md'),
    ('../style', 'SKILL.md'),
    ('style', '../other/SKILL.md'),
    ('style', '/etc/passwd'),
    ('style', '.hidden.md'),
    ('style', 'references/../../escape.md'),
])
def test_skill_read_stays_inside_a_known_skill(tmp_path, name, path):
    vault = make_vault(tmp_path)
    write_skill(vault, 'other', 'Another skill')
    write_skill(vault, 'style', 'Form of a note')
    with pytest.raises(VaultError):
        read_skill(vault, SKILLS, name, path)


def test_hidden_agent_paths_stay_closed_to_plain_reads(tmp_path):
    vault = make_vault(tmp_path)
    write_skill(vault, 'style', 'Form of a note')
    write_memory_note(vault, 'reference-a')
    hidden = vault.root / SKILLS / '.hidden'
    hidden.mkdir()
    (hidden / 'SKILL.md').write_text('---\nname: hidden\ndescription: Hidden\n---\n')
    for path in (f'{SKILLS}/style/SKILL.md', f'{MEMORY}/reference-a.md', f'{SKILLS}/.hidden/SKILL.md'):
        with pytest.raises(VaultError):
            vault.read(path)
    with pytest.raises(VaultError):
        vault.agent_read(f'{SKILLS}/.hidden/SKILL.md')


def test_memory_list_reads_notes_and_skips_the_index(tmp_path):
    vault = make_vault(tmp_path)
    content, _ = write_memory_note(vault, 'reference-a', description='First fact')
    directory = vault.root / MEMORY
    (directory / 'plain.md').write_text('No frontmatter\n')
    (directory / 'MEMORY.md').write_text('# Memory index\n')
    assert list_memory(vault, MEMORY) == {'memories': [
        {'name': 'plain', 'description': None, 'type': None, 'path': f'{MEMORY}/plain.md',
         'revision': sha256(b'No frontmatter\n').hexdigest()},
        {'name': 'reference-a', 'description': 'First fact', 'type': 'reference',
         'path': f'{MEMORY}/reference-a.md', 'revision': sha256(content.encode()).hexdigest()},
    ]}


def test_memory_list_of_a_missing_directory_is_empty(tmp_path):
    assert list_memory(make_vault(tmp_path), MEMORY) == {'memories': []}


def test_memory_read_accepts_a_stem_or_a_file_name(tmp_path):
    vault = make_vault(tmp_path)
    content, _ = write_memory_note(vault, 'reference-a')
    write_skill(vault, 'demo', 'Demo skill')
    assert read_memory(vault, MEMORY, 'reference-a') == {
        'path': f'{MEMORY}/reference-a.md', 'content': content, 'revision': sha256(content.encode()).hexdigest()}
    assert read_memory(vault, MEMORY, 'reference-a.md')['content'] == content
    for name in ('../reference-a', '../skills/demo/SKILL.md', '.hidden'):
        with pytest.raises(VaultError):
            read_memory(vault, MEMORY, name)


def test_memory_write_creates_the_note_and_the_index(tmp_path):
    vault = make_vault(tmp_path)
    content = ('---\nname: reference-quotes\ndescription: Straight quotes\n'
               'metadata:\n  type: reference\n---\nText\n')
    result = write_memory(vault, MEMORY, 'reference-quotes', content)
    assert result == {'path': f'{MEMORY}/reference-quotes.md', 'revision': sha256(content.encode()).hexdigest(),
                      'index_revision': sha256(
                          '# Memory index\n\n- [reference-quotes](reference-quotes.md) — Straight quotes\n'
                          .encode()).hexdigest()}
    assert (vault.root / MEMORY / 'reference-quotes.md').read_text() == content
    assert (vault.root / MEMORY / 'MEMORY.md').read_text() == (
        '# Memory index\n\n- [reference-quotes](reference-quotes.md) — Straight quotes\n')


def test_memory_write_keeps_an_existing_index_title(tmp_path):
    vault = make_vault(tmp_path)
    content, _ = write_memory_note(vault, 'reference-a', description='New summary')
    index = vault.root / MEMORY / 'MEMORY.md'
    index.write_text('# Memory index\n\n- [Hand title](reference-a.md) — Old summary\n'
                     '- [Other](reference-b.md) — Other summary\n')
    write_memory(vault, MEMORY, 'reference-a', content,
                 expected_revision=sha256(content.encode()).hexdigest())
    assert index.read_text() == ('# Memory index\n\n- [Hand title](reference-a.md) — New summary\n'
                                 '- [Other](reference-b.md) — Other summary\n')


def test_memory_write_needs_frontmatter_a_description_and_a_safe_name(tmp_path):
    vault = make_vault(tmp_path)
    with pytest.raises(VaultError, match='description'):
        write_memory(vault, MEMORY, 'note', 'No frontmatter\n')
    for name in ('MEMORY', '../escape', 'nested/note', '.hidden'):
        with pytest.raises(VaultError):
            write_memory(vault, MEMORY, name, '---\ndescription: A fact\n---\nBody\n')


def test_memory_write_checks_the_revision_before_replacing(tmp_path):
    vault = make_vault(tmp_path)
    content, _ = write_memory_note(vault, 'reference-a')
    with pytest.raises(VaultError, match='read it before'):
        write_memory(vault, MEMORY, 'reference-a', content)
    with pytest.raises(VaultError, match='revision'):
        write_memory(vault, MEMORY, 'reference-a', content, expected_revision='0' * 64)


def test_memory_write_ignores_note_rules(tmp_path):
    vault = make_vault(tmp_path, checks={'default_quotes': '""', 'trailing_periods': True})
    content = '---\nname: reference-a\ndescription: A fact\n---\nQuote "here".\n'
    assert write_memory(vault, MEMORY, 'reference-a', content)['revision']
