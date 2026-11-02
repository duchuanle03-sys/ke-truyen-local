from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

from tts import TTSError, capabilities, synthesize, voices


class TTSTests(unittest.TestCase):
    def test_invalid_text_fails_before_starting_a_process(self):
        with tempfile.TemporaryDirectory() as directory, patch('tts.subprocess.run', side_effect=AssertionError('process must not start')):
            for value in ('', '  ', None, 'x' * 50001, 'bad\x00text', '\ud800'):
                with self.subTest(value=str(value)[:20]), self.assertRaises(TTSError):
                    synthesize(value, Path(directory) / 'speech.wav')

    def test_non_windows_reports_unavailable_and_does_not_launch(self):
        with patch('tts.sys.platform', 'linux'), patch('tts.subprocess.run', side_effect=AssertionError('process must not start')):
            self.assertFalse(capabilities()['supported'])
            with self.assertRaisesRegex(TTSError, 'Windows'):
                voices()

    def test_malformed_engine_output_is_not_success(self):
        class Result:
            stdout = b'{"voices": "invalid"}'
            stderr = b''
            returncode = 0
        with patch('tts.sys.platform', 'win32'), patch('tts.subprocess.run', return_value=Result()), self.assertRaises(TTSError):
            voices()

    def test_text_is_utf8_file_input_and_failed_export_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'speech.wav'
            output.write_bytes(b'original')
            def run(command, **kwargs):
                text_file = Path(command[command.index('-TextPath') + 1])
                self.assertEqual(text_file.read_text(encoding='utf-8'), 'Tiếng Việt & $(never-execute)')
                self.assertNotIn('Tiếng Việt', ' '.join(command))
                class Result:
                    stdout = b'{"error":"test synthesis failure"}'
                    stderr = b''
                    returncode = 1
                return Result()
            with patch('tts.sys.platform', 'win32'), patch('tts.subprocess.run', side_effect=run), self.assertRaisesRegex(TTSError, 'test synthesis failure'):
                synthesize('Tiếng Việt & $(never-execute)', output)
            self.assertEqual(output.read_bytes(), b'original')
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_valid_wav_is_published_after_engine_success(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'speech.wav'
            output.write_bytes(b'old recording')
            def run(command, **kwargs):
                temporary_audio = command[command.index('-OutputPath') + 1]
                self.assertNotEqual(Path(temporary_audio), output)
                with wave.open(temporary_audio, 'wb') as audio:
                    audio.setparams((1, 2, 8000, 0, 'NONE', 'not compressed'))
                    audio.writeframes(b'\x00\x01' * 800)
                class Result:
                    stdout = b'{"written":true}'
                    stderr = b''
                    returncode = 0
                return Result()
            with patch('tts.subprocess.run', side_effect=run):
                self.assertEqual(synthesize('Hello.', output), output.resolve())
            with wave.open(str(output), 'rb') as audio:
                self.assertEqual(audio.getnframes(), 800)
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_native_engine_error_is_reported_as_unavailable(self):
        with patch('tts._invoke', side_effect=TTSError('Internal Speech Error (0x800455A0)')):
            status = capabilities()
            self.assertFalse(status['supported'])
            self.assertEqual(status['voices'], [])
            self.assertIn('0x800455A0', status['error'])

    @unittest.skipUnless(sys.platform == 'win32', 'Windows WinRT speech integration')
    def test_actual_installed_voice_synthesizes_valid_nonempty_wav_without_playback(self):
        status = capabilities()
        if not status['supported']:
            self.skipTest('Native Windows voice unavailable: ' + status['error'])
        installed = status['voices']
        first = installed[0]
        self.assertTrue(first['offline'])
        self.assertTrue(first['language'])
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'native-voice.wav'
            self.assertEqual(synthesize('Hello. This is the local reader.', output, first['id']), output.resolve())
            with wave.open(str(output), 'rb') as audio:
                self.assertGreater(audio.getnframes(), 0)
                self.assertGreater(audio.getnframes() / audio.getframerate(), 0.2)
                self.assertGreater(audio.getnchannels(), 0)
                self.assertGreater(audio.getsampwidth(), 0)
            with self.assertRaisesRegex(TTSError, 'voice|giọng'):
                synthesize('Hello.', Path(directory) / 'missing.wav', 'missing-test-voice')
            self.assertFalse((Path(directory) / 'missing.wav').exists())


if __name__ == '__main__':
    unittest.main()
