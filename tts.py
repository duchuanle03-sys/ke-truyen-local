"""Export WAV with Windows' installed offline WinRT speech voices."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import wave


class TTSError(ValueError):
    pass


SCRIPT = Path(__file__).parent / 'windows' / 'tts.ps1'
MAX_TEXT_CHARACTERS = 50000
MAX_WAV_BYTES = 256 * 1024 * 1024


def _invoke(action, text_path=None, output_path=None, voice_id=''):
    if sys.platform != 'win32':
        raise TTSError('Giọng offline WinRT chỉ có trên Windows.')
    executable = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
    command = [str(executable), '-NoLogo', '-NoProfile', '-NonInteractive', '-File', str(SCRIPT), '-Action', action]
    if text_path is not None:
        command.extend(['-TextPath', str(text_path), '-OutputPath', str(output_path)])
    if voice_id:
        command.extend(['-VoiceId', voice_id])
    try:
        result = subprocess.run(command, capture_output=True, timeout=30 if action == 'Voices' else 300,
            creationflags=subprocess.CREATE_NO_WINDOW, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TTSError('Không chạy được giọng offline Windows: ' + str(exc)) from exc
    try:
        payload = json.loads(result.stdout.decode('utf-8-sig'))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise TTSError('Engine giọng Windows không trả kết quả hợp lệ: ' + result.stderr.decode('utf-8', errors='replace')[-1000:]) from exc
    if result.returncode or not isinstance(payload, dict) or payload.get('error'):
        detail = str(payload.get('error', 'engine failed')) + (' (' + str(payload['hresult']) + ')' if payload.get('hresult') else '') if isinstance(payload, dict) else 'invalid engine payload'
        raise TTSError('Giọng Windows: ' + detail)
    return payload


def voices():
    installed = _invoke('Voices').get('voices')
    if not isinstance(installed, list) or any(not isinstance(voice, dict) or any(not isinstance(voice.get(key), str) or not voice[key] for key in ('id', 'name', 'language', 'gender')) for voice in installed):
        raise TTSError('Engine không trả danh sách giọng hợp lệ.')
    return [{**voice, 'offline': True} for voice in installed]


def capabilities():
    try:
        installed = voices()
        error = '' if installed else 'Chưa cài giọng Windows dùng được. Thêm giọng trong Windows Settings > Time & language > Speech.'
    except TTSError as exc:
        installed, error = [], str(exc)
    return {'engine': 'Windows.Media.SpeechSynthesis', 'offline': True, 'supported': bool(installed),
        'voices': installed, 'error': error, 'format': 'wav', 'max_text_characters': MAX_TEXT_CHARACTERS}


def synthesize(text, output_path, voice_id=''):
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARACTERS or '\x00' in text:
        raise TTSError('Văn bản giọng đọc phải có từ 1 đến 50.000 ký tự và không chứa ký tự NUL.')
    try:
        text.encode('utf-8')
    except UnicodeError as exc:
        raise TTSError('Văn bản giọng đọc không phải Unicode hợp lệ.') from exc
    if not isinstance(voice_id, str) or len(voice_id) > 2048 or any(ord(char) < 32 for char in voice_id):
        raise TTSError('Định danh giọng đọc không hợp lệ.')
    try:
        output = Path(output_path).resolve()
    except (TypeError, ValueError, OSError) as exc:
        raise TTSError('Đường dẫn WAV không hợp lệ.') from exc
    if output.suffix.lower() != '.wav' or output.is_dir():
        raise TTSError('Chọn đường dẫn tệp WAV hợp lệ.')
    text_path = temporary_audio = None
    try:
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', newline='', prefix='.speech-', suffix='.txt', dir=output.parent, delete=False) as handle:
            text_path = Path(handle.name)
            handle.write(text)
        with tempfile.NamedTemporaryFile(prefix='.speech-', suffix='.wav', dir=output.parent, delete=False) as handle:
            temporary_audio = Path(handle.name)
        payload = _invoke('Synthesize', text_path, temporary_audio, voice_id)
        if payload.get('written') is not True or not temporary_audio.is_file() or not 44 < temporary_audio.stat().st_size <= MAX_WAV_BYTES:
            raise TTSError('Engine không tạo tệp WAV hợp lệ.')
        with wave.open(str(temporary_audio), 'rb') as audio:
            if audio.getnframes() < 1 or audio.getframerate() < 1 or audio.getnchannels() < 1:
                raise TTSError('Engine trả âm thanh trống.')
        os.replace(temporary_audio, output)
        return output
    except (OSError, EOFError, wave.Error) as exc:
        raise TTSError('Không ghi được âm thanh WAV: ' + str(exc)) from exc
    finally:
        if text_path is not None:
            text_path.unlink(missing_ok=True)
        if temporary_audio is not None:
            temporary_audio.unlink(missing_ok=True)
