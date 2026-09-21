"""Bounded system-playback capture through the existing optional macOS helper."""

import hashlib
import math
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue

from . import audio_status
from .files import absolute_path, read_bytes
from .models import Contract


class AudioCapture(Contract):
    source: Literal['system']
    seconds: int = Field(ge=1, le=30, strict=True)
    output_directory: str = Field(min_length=1, max_length=4096)


class AudioCaptureUnknown(RuntimeError):
    def __init__(self, directory: Path) -> None:
        super().__init__('Audio capture did not return a verified result; inspect partial files')
        self.directory = str(directory)


async def capture_audio(args: AudioCapture) -> dict[str, JsonValue]:
    directory = absolute_path(args.output_directory)
    if directory.exists() or directory.is_symlink() or not directory.parent.is_dir():
        raise ValueError('Audio capture requires a new output directory under an existing parent')
    directory = directory.parent.resolve() / directory.name
    status = await audio_status.inspect_audio()
    if status.get('state') != 'available' or not status.get('screen_capture_allowed'):
        raise ValueError('System audio capture requires the installed macOS helper and permission')
    helper = audio_status.verified_audio_helper()
    if helper is None:
        raise ValueError('Audio helper is no longer installed')
    try:
        result = await audio_status._query(
            [str(helper), 'system', str(args.seconds), str(directory)],
            timeout=args.seconds + 20,
        )
        if (not isinstance(result, dict) or result.get('state') != 'captured'
                or result.get('source') != 'system'
                or result.get('microphone_device_id') != 'none'
                or result.get('output_directory') != str(directory)
                or result.get('requested_seconds') != args.seconds):
            raise ValueError('Invalid capture receipt')
        frames = result['frames']['system']
        metrics = result['measurements']['system']
        if (type(frames) is not int or frames <= 0 or not isinstance(metrics, dict)
                or any(type(metrics.get(key)) not in (int, float)
                       or not math.isfinite(metrics[key]) for key in
                       ('sample_rate', 'duration_seconds', 'peak', 'rms'))):
            raise ValueError('Invalid capture measurements')
        rate, duration = metrics['sample_rate'], metrics['duration_seconds']
        if (rate <= 0 or not 0 < duration <= args.seconds
                or frames > args.seconds * rate or abs(duration - frames / rate) > 1e-6
                or not 0 <= metrics['rms'] <= metrics['peak']):
            raise ValueError('Inconsistent capture measurements')
        artifact = directory / 'system.caf'
        if directory.is_symlink() or artifact.is_symlink():
            raise ValueError('Capture artifact must not be a symlink')
        data = read_bytes(artifact)
        if not data.startswith(b'caff'):
            raise ValueError('Capture artifact is not a CAF file')
        return {'state': 'captured', 'source': 'system', 'microphone_used': False,
                'requested_seconds': args.seconds, 'frames': frames,
                'sample_rate': rate, 'duration_seconds': duration,
                'peak': metrics['peak'], 'rms': metrics['rms'],
                'measurements_source': 'native_helper', 'speaker_output_verified': False,
                'artifact': {'path': str(artifact), 'size': len(data),
                             'sha256': hashlib.sha256(data).hexdigest(),
                             'media_type': 'audio/x-caf'}}
    except (OSError, ValueError, TypeError, KeyError, TimeoutError) as error:
        raise AudioCaptureUnknown(directory) from error
