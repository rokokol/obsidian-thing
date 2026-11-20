import json
import subprocess
import sys
from pathlib import Path

import pytest

from obsidian_thing.checker import Checker

SCRIPT = Path(__file__).parents[1] / 'examples' / 'checker.py'


@pytest.fixture
def checker():
    return Checker({'script': str(SCRIPT.resolve())})


def test_example_allows_a_clean_note(checker):
    text = '$$\nx\n$$\n\n> $$\n> x\n> $$\n\n*курсив* **жирный** `$$x$$`\n\n```md\n$$x$$\n_text_\n```\n'
    assert checker.check('write', 'note.md', None, text) == []


def test_example_warns_about_new_layout_mistakes(checker):
    warnings = checker.check('update', 'note.md', '*текст*\n', '_текст_\n\n$$x$$\n')
    assert len(warnings) == 2
    assert any('emphasis-asterisk' in warning for warning in warnings)
    assert any('math-block-layout' in warning for warning in warnings)


def test_example_marks_old_findings(checker):
    text = '$$x$$\n'
    warnings = checker.check('update', 'note.md', text, text + '\nНовый абзац\n')
    assert len(warnings) == 1 and warnings[0].startswith('Existing: ')


def test_example_ignores_other_extensions(checker):
    assert checker.check('write', 'note.txt', None, '$$x$$\n_text_\n') == []


def test_example_runs_without_arguments():
    request = json.dumps({'version': 1, 'operation': 'write', 'path': 'note.md', 'before': None,
                          'after': '$$x$$\n'}).encode()
    result = subprocess.run([sys.executable, '-P', '-s', str(SCRIPT)], input=request,
                            capture_output=True, check=True)
    assert json.loads(result.stdout)['decision'] == 'allow'
