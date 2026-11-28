import pytest

from obsidian_thing.embeddings import Embedder
from obsidian_thing.search import SearchIndex
from obsidian_thing.search_attachments import attachment_parts
from obsidian_thing.search_chunks import chunks
from obsidian_thing.vault import Vault, VaultError


def test_a_heading_without_body_produces_no_chunk():
    text = '# Календарь\n\n## Вторник\n\nПлотный абзац из восьми слов подряд без лишних\n\n# Пустой\n'
    found = [(chunk.headings, chunk.words) for chunk in chunks(text, 1500, markdown=True, sections=True)]
    assert found == [(('Календарь', 'Вторник'), 8)]


def test_properties_alone_stay_one_chunk_of_zero_words():
    text = '---\ntags: [daily]\n---\n'
    found = list(chunks(text, 1500, markdown=True, sections=True))
    assert [(chunk.headings, chunk.words) for chunk in found] == [((), 0)]


def test_min_chunk_words_keeps_short_bodies_in_text_search_only(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'long.md').write_text('# Тема\n\nдостаточно длинный абзац из нескольких слов для вектора\n')
    (root / 'short.md').write_text('# Тема\n\nдва слова\n')
    monkeypatch.setattr(Embedder, 'embed', lambda self, text, operation: [1.0, 0.0])
    index = SearchIndex(Vault(root, ['.']), dict(provider='ollama', model='synthetic', dimensions=2,
                        min_chunk_words=5), tmp_path)
    assert index.index('long.md')['chunks'] == 1
    assert index.index('short.md')['chunks'] == 0
    assert 'short.md' in [hit['path'] for hit in index.query('два', 10)['matches']]
    assert index.related('long.md', 10, -1)['matches'] == []
    assert index.pending() == []


def test_the_default_keeps_twenty_four_word_bodies(tmp_path):
    (tmp_path / 'vault').mkdir()
    index = SearchIndex(Vault(tmp_path / 'vault', ['.']), dict(provider='ollama', model='synthetic',
                        dimensions=2), tmp_path)
    assert index.min_chunk_words == 24


def test_zero_restores_the_previous_behaviour(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'long.md').write_text('# Тема\n\nдостаточно длинный абзац из нескольких слов для вектора\n')
    (root / 'short.md').write_text('# Тема\n\nдва слова\n')
    monkeypatch.setattr(Embedder, 'embed', lambda self, text, operation: [1.0, 0.0])
    index = SearchIndex(Vault(root, ['.']), dict(provider='ollama', model='synthetic', dimensions=2,
                        min_chunk_words=0), tmp_path)
    assert index.index('short.md')['chunks'] == 1


def test_related_names_the_limit_when_the_source_is_too_short(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'short.md').write_text('# Тема\n\nдесять слов это ровно десять слов в этом абзаце вот\n')
    monkeypatch.setattr(Embedder, 'embed', lambda self, text, operation: [1.0, 0.0])
    index = SearchIndex(Vault(root, ['.']), dict(provider='ollama', model='synthetic', dimensions=2),
                        tmp_path)
    assert index.index('short.md')['chunks'] == 0
    with pytest.raises(VaultError, match='at least 24 words'):
        index.related('short.md', 10, -1)
    with pytest.raises(VaultError, match='at least 24 words'):
        index.map('short.md', 10, -1)


@pytest.mark.parametrize('value', [-1, 1001, 1.5, '5', True])
def test_min_chunk_words_must_be_an_integer_in_range(tmp_path, value):
    (tmp_path / 'vault').mkdir()
    with pytest.raises(ValueError, match='min_chunk_words'):
        SearchIndex(Vault(tmp_path / 'vault', ['.']), dict(provider='ollama', model='synthetic',
                    dimensions=2, min_chunk_words=value), tmp_path)


def test_related_names_the_section_when_a_selected_section_is_too_short(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'note.md').write_text('# Long\n\n' + ' '.join(['word'] * 30) + '\n\n# Short\n\ntwo words\n')
    monkeypatch.setattr(Embedder, 'embed', lambda self, text, operation: [1.0, 0.0])
    index = SearchIndex(Vault(root, ['.']), dict(provider='ollama', model='synthetic', dimensions=2),
                        tmp_path)
    assert index.index('note.md')['chunks'] == 1
    with pytest.raises(VaultError, match='selected section has no fragments of at least 24 words'):
        index.related('note.md', 10, -1, section_line=5)


def test_a_vector_written_as_json_is_packed_at_startup_and_a_broken_row_is_dropped(tmp_path):
    import sqlite3
    root = tmp_path / 'vault'
    root.mkdir()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    index = SearchIndex(Vault(root, ['.']), dict(provider='ollama', model='synthetic', dimensions=2), state)
    assert index.embedder is not None
    profile = index.embedder.profile
    with sqlite3.connect(index.database) as db:
        db.execute('INSERT INTO vectors VALUES (?, ?, ?)', (profile, 'old', '[1.0, 0.0]'))
        db.execute('INSERT INTO vectors VALUES (?, ?, ?)', (profile, 'broken', 'not json'))
    reopened = SearchIndex(Vault(root, ['.']), dict(provider='ollama', model='synthetic', dimensions=2), state)
    with sqlite3.connect(reopened.database) as db:
        rows = dict(db.execute('SELECT hash, typeof(vector) FROM vectors').fetchall())
    assert rows == {'old': 'blob'}


def _pdf(pages: int) -> bytes:
    from io import BytesIO
    from pypdf import PdfWriter
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_pdf_page_limit_names_the_counts_and_follows_the_setting(tmp_path):
    root = tmp_path / 'vault'
    root.mkdir()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    (root / 'book.pdf').write_bytes(_pdf(29))
    config = dict(provider='ollama', model='synthetic', dimensions=2, max_pdf_pages=16)
    index = SearchIndex(Vault(root, ['.']), config, state)
    with pytest.raises(VaultError, match='has 29 pages; the configured limit is 16'):
        list(attachment_parts('application/pdf', (root / 'book.pdf').read_bytes(), max_pdf_pages=16))
    with pytest.raises(ValueError, match='max_pdf_pages'):
        SearchIndex(Vault(root, ['.']), config | {'max_pdf_pages': 0}, tmp_path / 'other')
    assert len(list(attachment_parts('application/pdf', (root / 'book.pdf').read_bytes(),
                                     max_pdf_pages=29))) == 29
    assert index.max_pdf_pages == 16
