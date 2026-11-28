import pytest

from obsidian_thing.embeddings import Embedder
from obsidian_thing.search import SearchIndex
from obsidian_thing.search_chunks import chunks
from obsidian_thing.vault import Vault


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


def test_the_default_keeps_eight_word_bodies(tmp_path):
    (tmp_path / 'vault').mkdir()
    index = SearchIndex(Vault(tmp_path / 'vault', ['.']), dict(provider='ollama', model='synthetic',
                        dimensions=2), tmp_path)
    assert index.min_chunk_words == 8


def test_zero_restores_the_previous_behaviour(tmp_path, monkeypatch):
    root = tmp_path / 'vault'
    root.mkdir()
    (root / 'long.md').write_text('# Тема\n\nдостаточно длинный абзац из нескольких слов для вектора\n')
    (root / 'short.md').write_text('# Тема\n\nдва слова\n')
    monkeypatch.setattr(Embedder, 'embed', lambda self, text, operation: [1.0, 0.0])
    index = SearchIndex(Vault(root, ['.']), dict(provider='ollama', model='synthetic', dimensions=2,
                        min_chunk_words=0), tmp_path)
    assert index.index('short.md')['chunks'] == 1


@pytest.mark.parametrize('value', [-1, 1001, 1.5, '5', True])
def test_min_chunk_words_must_be_an_integer_in_range(tmp_path, value):
    (tmp_path / 'vault').mkdir()
    with pytest.raises(ValueError, match='min_chunk_words'):
        SearchIndex(Vault(tmp_path / 'vault', ['.']), dict(provider='ollama', model='synthetic',
                    dimensions=2, min_chunk_words=value), tmp_path)
