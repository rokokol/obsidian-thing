import json
import os
import selectors
import signal
import subprocess
import sys
from contextlib import suppress
from pathlib import Path
from time import monotonic


class CheckerError(ValueError):
    pass


class Checker:
    max_output_bytes = 16_384

    def __init__(self, config: object):
        keys = {'script', 'timeout_seconds', 'vault_context_properties'}
        if not isinstance(config, dict) or config.keys() - keys:
            raise ValueError('Configure checker with script, optional timeout_seconds and vault_context_properties')
        properties = config.get('vault_context_properties', [])
        if (not isinstance(properties, list) or len(properties) > 32
                or any(not isinstance(key, str) or not key.strip() or len(key) > 128
                       for key in properties) or len(set(properties)) != len(properties)):
            raise ValueError('Set checker.vault_context_properties to a list of distinct property names')
        self.vault_context_properties: frozenset[str] = frozenset(properties)
        script = config.get('script')
        if not isinstance(script, str) or not Path(script).is_absolute():
            raise ValueError('Configure checker.script as an absolute path')
        self.script = Path(script).resolve()
        if not self.script.is_file():
            raise ValueError('The checker script must be a regular file')
        timeout = config.get('timeout_seconds', 2)
        if type(timeout) not in (int, float) or not 0 < timeout <= 30:
            raise ValueError('Set checker.timeout_seconds above zero and at most 30')
        self.timeout = timeout

    def _run(self, payload: bytes) -> bytearray:
        deadline = monotonic() + self.timeout
        # A Nix wrapper adds packages through site.addsitedir inside its own process, so a child
        # interpreter inherits none of them. Pass those directories explicitly, and nothing else.
        packages = [path for path in sys.path if path and path.endswith('site-packages')]
        environment = {'PYTHONPATH': os.pathsep.join(packages)} if packages else {}
        with subprocess.Popen(
            [sys.executable, '-s', '-P', '-X', 'utf8', str(self.script)], cwd=self.script.parent,
            env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            start_new_session=True,
        ) as process, selectors.DefaultSelector() as pipes:
            try:
                assert process.stdin is not None and process.stdout is not None
                for stream, event in ((process.stdin, selectors.EVENT_WRITE), (process.stdout, selectors.EVENT_READ)):
                    os.set_blocking(stream.fileno(), False)
                    pipes.register(stream, event)
                pending = memoryview(payload)
                output = bytearray()
                while pipes.get_map():
                    remaining = deadline - monotonic()
                    if remaining <= 0 or not (events := pipes.select(remaining)):
                        raise CheckerError('The write checker timed out')
                    for key, event in events:
                        if event == selectors.EVENT_WRITE:
                            pending = pending[os.write(key.fd, pending):]
                            if not pending:
                                pipes.unregister(key.fd)
                                process.stdin.close()
                        else:
                            block = os.read(key.fd, self.max_output_bytes + 1 - len(output))
                            if not block:
                                pipes.unregister(key.fd)
                            output.extend(block)
                            if len(output) > self.max_output_bytes:
                                raise CheckerError('The write checker response is too large')
                if process.wait(timeout=max(0, deadline - monotonic())) != 0:
                    raise CheckerError('The write checker failed')
                return output
            finally:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)

    def check(self, operation: str, path: str, before: str | None, after: str,
              links: list[dict[str, object]] | None = None, *,
              vault_context: dict[str, object] | None = None) -> list[str]:
        request: dict[str, object] = dict(version=1, operation=operation, path=path, before=before,
                                         after=after, links=links or [])
        if vault_context is not None:
            request['vault_context'] = vault_context
        payload = json.dumps(request).encode()
        try:
            output = self._run(payload)
        except subprocess.TimeoutExpired:
            raise CheckerError('The write checker timed out') from None
        except OSError:
            raise CheckerError('The write checker failed') from None
        try:
            result = json.loads(output.decode('utf-8'))
        except (ValueError, RecursionError):
            raise CheckerError('The write checker returned an invalid response') from None
        if isinstance(result, dict):
            decision = result.get('decision')
            if decision == 'allow' and result.keys() <= {'decision', 'warnings'}:
                warnings = result.get('warnings', [])
                if isinstance(warnings, list) and all(self._valid_message(item) for item in warnings):
                    return warnings
            if decision == 'reject' and result.keys() == {'decision', 'message'}:
                message = result['message']
                if self._valid_message(message):
                    raise CheckerError(f'The write checker rejected the change: {message}')
        raise CheckerError('The write checker returned an invalid response')

    @staticmethod
    def _valid_message(value: object) -> bool:
        return isinstance(value, str) and bool(value.strip()) and value.isprintable()
