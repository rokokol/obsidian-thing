import errno
from functools import lru_cache

from pathspec import GitIgnoreSpec

from .vault import Vault, VaultError, _parts


@lru_cache(maxsize=64)
def _parse(text: str) -> GitIgnoreSpec:
    return GitIgnoreSpec.from_lines(text.splitlines())


class IndexPolicy:
    def __init__(self, vault: Vault, settings: object):
        if not isinstance(settings, dict) or settings.keys() - {'exclude', 'respect_gitignore'}:
            raise ValueError('Configure indexing as a table with supported keys')
        patterns = settings.get('exclude', ['*.tmp', '*.swp', '*~'])
        if not isinstance(patterns, list) or not all(isinstance(line, str) and '\n' not in line and
                                                     '\r' not in line and '\0' not in line for line in patterns):
            raise ValueError('Configure indexing.exclude as a list of single-line patterns')
        self.exclude = GitIgnoreSpec.from_lines(patterns)
        respect = settings.get('respect_gitignore', False)
        if not isinstance(respect, bool):
            raise ValueError('Configure indexing.respect_gitignore as a boolean')
        self.respect_gitignore = respect
        self.vault = vault

    def _ignore_at(self, directory: tuple[str, ...]) -> GitIgnoreSpec | None:
        name = (*directory, '.gitignore')
        try:
            with self.vault._open(name) as fd:
                note, _ = self.vault._read_text('/'.join(name), fd)
            return _parse(note['content'])
        except FileNotFoundError:
            return None
        except OSError as error:
            if error.errno == errno.ELOOP:
                return None
            raise VaultError('Cannot read the indexing ignore rules') from None
        except ValueError:
            raise VaultError('Cannot parse the indexing ignore rules') from None

    def check(self, path: str) -> None:
        parts = _parts(path)
        rules: list[tuple[int, GitIgnoreSpec]] = []
        for depth in range(len(parts)):
            relative = '/'.join(parts[:depth + 1])
            suffix = '/' if depth + 1 < len(parts) else ''
            if self.exclude.match_file(relative + suffix):
                raise VaultError('The path is excluded from indexing')
            if not self.respect_gitignore:
                continue
            spec = self._ignore_at(parts[:depth])
            if spec is not None:
                rules.append((depth, spec))
            for base, spec in reversed(rules):
                match = spec.check_file('/'.join(parts[base:depth + 1]) + suffix).include
                if match is True:
                    raise VaultError('The path is excluded from indexing by .gitignore')
                if match is False:
                    break
