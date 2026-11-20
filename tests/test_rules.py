from pathlib import Path

import pytest

from obsidian_thing.rules import compare, scan


OPTIONS: dict[str, object] = {
    'inline_code_equals': True,
    'heading_math': True,
    'unicode_math': True,
    'link_math': True,
    'paragraph_lines': True,
}


def codes(text: str) -> list[tuple[str, str]]:
    return [(item.code, item.payload) for item in scan('note.md', text, OPTIONS, None)]


def test_inline_code_equals_is_an_error():
    assert ('inline_code_equals', '= b') in codes('Строка с `= b` внутри\n')
    assert ('inline_code_equals', '= x + 1') in codes('Формула `= x + 1`\n')
    assert codes('Обычный `a = b` код\n') == []
    assert codes('```\n= b\n```\n') == []
    assert codes('$$= b$$\n') == []


def test_unicode_math_is_an_error_in_a_heading_and_a_warning_in_prose():
    heading = codes('## Предел x ≤ 0\n')
    assert heading == [('heading_math', '≤')]
    prose = codes('Предел x ≤ 0 в прозе\n')
    assert prose == [('unicode_math', '≤')]
    assert codes('## Предел $x \\le 0$\n') == []
    assert codes('Предел $x \\le 0$ в прозе\n') == []


def test_a_link_keeps_unicode_math_and_rejects_tex():
    assert codes('[[Файл|предел ≤ 0]]\n') == []
    assert codes('## Предел [[Файл|≤ 0]]\n') == []
    assert codes('[[Файл|x $x^2$]]\n') == [('link_math', '[[Файл|x $x^2$]]')]
    assert codes('[[Файл#$x^2$]]\n') == [('link_math', '[[Файл#$x^2$]]')]
    assert codes('![[img.png|$x$]]\n') == [('link_math', '![[img.png|$x$]]')]
    assert codes('`[[Файл|$x$]]`\n') == []


def test_hard_wrapped_paragraph_is_a_warning():
    wrapped = 'Первый абзац\nпродолжение строки\n'
    found = codes(wrapped)
    assert [code for code, _ in found] == ['paragraph_lines']
    assert codes('Первый абзац\n\nВторой абзац\n') == []
    assert codes('- пункт один\n- пункт два\n') == []
    assert [code for code, _ in codes('- пункт\n  продолжение\n')] == ['paragraph_lines']


def test_old_findings_warn_and_new_ones_reject():
    before = 'Строка с `= b` внутри\n\nАбзац\nперенос\n'
    after = before + '\nЕщё `= c`\n'
    errors, warnings = compare(before, after, 'note.md', OPTIONS, None)
    assert any('inline_code_equals' in error and '= c' in error for error in errors)
    assert any('Existing' in warning for warning in warnings)


def test_options_are_independent():
    text = '## Раздел x ≤ 0\n\n`= b`\n'
    assert {code for code, _ in codes(text)} == {'heading_math', 'inline_code_equals'}
    only_math: dict[str, object] = dict(OPTIONS, inline_code_equals=False, paragraph_lines=False,
                                       unicode_math=False)
    assert [item.code for item in scan('note.md', text, only_math, None)] == ['heading_math']


def test_other_extensions_are_untouched():
    assert scan('note.txt', '## Раздел x ≤ 0\n`= b`\n', OPTIONS, None) == []


@pytest.mark.parametrize('text', ['Готово\n', '## Заголовок\n\nАбзац\n'])
def test_clean_note_has_no_findings(text):
    assert codes(text) == []
