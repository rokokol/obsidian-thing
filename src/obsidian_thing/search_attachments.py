import json
import math
import shutil
import subprocess
from io import BytesIO
from tempfile import TemporaryFile
from pathlib import PurePosixPath
from typing import Iterator

from PIL import Image
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PyPdfError

from .vault import VaultError

TEXT_TYPES = frozenset(('text/markdown', 'text/plain'))
SUFFIX_TYPES = {
    '.md': 'text/markdown',
    '.txt': 'text/plain',
    '.png': 'image/png',
    '.jpg': 'image/jpeg',
    '.jpeg': 'image/jpeg',
    '.pdf': 'application/pdf',
    '.wav': 'audio/wav',
    '.mp3': 'audio/mpeg',
    '.mp4': 'video/mp4',
}
MEDIA_FORMATS = {
    'audio/wav': ('wav', 180),
    'audio/mpeg': ('mp3', 180),
    'video/mp4': ('mp4', 120),
}
ATTACHMENT_TYPES = frozenset(SUFFIX_TYPES.values()) - TEXT_TYPES
ATTACHMENT_LIMIT = 5 * 1024 * 1024
PDF_PAGE_LIMIT = 16
MAX_IMAGE_PIXELS = 16_000_000


def mime_type(path: str) -> str | None:
    return SUFFIX_TYPES.get(PurePosixPath(path).suffix.lower())


def _validate_media(mime: str, data: bytes) -> float | int | None:
    format_name, limit = MEDIA_FORMATS[mime]
    video = mime.startswith('video/')
    if video and (data[4:8] != b'ftyp' or len(data) < 16
                  or data[8:12] not in (b'isom', b'iso2', b'iso4', b'iso5', b'iso6', b'mp41', b'mp42',
                                        b'avc1', b'av01', b'M4V ', b'dash', b'MSNV')):
        raise VaultError('Use an MP4 container with a supported file type')
    ffprobe = shutil.which('ffprobe')
    if ffprobe is None:
        raise VaultError('Install ffprobe to validate audio and video attachments')
    command = [ffprobe, '-v', 'error', '-protocol_whitelist', 'fd', '-format_whitelist', 'wav,mp3,mov',
               '-max_alloc', '33554432', '-probesize', str(ATTACHMENT_LIMIT)]
    if video:
        command += ['-enable_drefs', '0', '-use_absolute_path', '0']
    command += ['-show_entries', 'format=format_name,duration:stream=codec_type,codec_name,duration,nb_frames'
                ':stream_disposition=attached_pic', '-of', 'json', '-i', 'fd:']
    try:
        with TemporaryFile() as sample:
            sample.write(data)
            sample.seek(0)
            result = subprocess.run(command, stdin=sample, capture_output=True, check=True, timeout=5)
        if result.stderr:
            raise ValueError
        info = json.loads(result.stdout)
        container = info['format']
        if format_name not in container['format_name'].split(','):
            raise ValueError
        duration = float(container['duration'])
        streams = [stream for stream in info['streams'] if not stream.get('disposition', {}).get('attached_pic')]
        expected = [stream for stream in streams if stream['codec_type'] == ('video' if video else 'audio')]
        if not expected or (video and any(s['codec_name'] not in ('h264', 'hevc', 'av1', 'vp9') for s in expected)):
            raise ValueError
        durations = [duration, *(float(s['duration']) for s in streams if 'duration' in s)]
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        raise VaultError('The attachment is not supported audio or video') from None
    if not all(math.isfinite(value) and 0 < value <= limit for value in durations):
        raise VaultError('The media duration is unknown or exceeds the limit')
    if not video:
        return max(durations)
    counts = [str(stream.get('nb_frames', '')) for stream in expected]
    return sum(map(int, counts)) if all(repr_count.isascii() and repr_count.isdecimal() and int(repr_count) > 0
                                        for repr_count in counts) else None


def attachment_parts(mime: str, data: bytes) -> Iterator[bytes]:
    if mime in MEDIA_FORMATS:
        _validate_media(mime, data)
        yield data
        return
    if mime == 'application/pdf':
        try:
            reader = PdfReader(BytesIO(data), strict=True)
            if reader.is_encrypted or not 1 <= len(reader.pages) <= PDF_PAGE_LIMIT:
                raise VaultError('Use an unencrypted PDF within the page limit')
            total_size = 0
            for page in reader.pages:
                writer = PdfWriter()
                writer.add_page(page, excluded_keys=('/Annots', '/AA', '/Metadata'))
                buffer = BytesIO()
                writer.write(buffer)
                if buffer.tell() > ATTACHMENT_LIMIT:
                    raise VaultError('A PDF page exceeds the attachment size limit')
                total_size += buffer.tell()
                if total_size > 2 * ATTACHMENT_LIMIT:
                    raise VaultError('The split PDF exceeds the total payload limit')
                yield buffer.getvalue()
        except (PyPdfError, OSError, ValueError, RecursionError, TypeError, IndexError, KeyError) as error:
            if isinstance(error, VaultError):
                raise
            raise VaultError('The attachment is not a supported PDF') from None
        return

    expected = {'image/png': 'PNG', 'image/jpeg': 'JPEG'}[mime]
    try:
        with Image.open(BytesIO(data), formats=[expected]) as image:
            if image.width * image.height > MAX_IMAGE_PIXELS:
                raise VaultError('The image exceeds the pixel limit')
            if getattr(image, 'n_frames', 1) != 1:
                raise VaultError('Only single-frame images can use this index')
            image.verify()
        if expected == 'JPEG':
            with Image.open(BytesIO(data), formats=[expected]) as image:
                image.load()
    except (OSError, SyntaxError, Image.DecompressionBombError):
        raise VaultError('The image does not match the expected format or is damaged') from None
    yield data
