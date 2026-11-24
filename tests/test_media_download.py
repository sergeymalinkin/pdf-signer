"""Offline transport tests with fictional bytes; no PDF/facsimile or MAX access."""
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from max_api import MaxAPI


class Response(io.BytesIO):
    def __init__(self, data, headers=None):
        super().__init__(data)
        self.headers = headers or {}


class DownloadTests(unittest.TestCase):
    def download(self, target, response, limit=16):
        api = MaxAPI.__new__(MaxAPI)
        api.opener = Mock()
        api.opener.open.return_value = response
        with patch('max_api.validate_media_url', return_value='https://test.max.ru/file'):
            api.download('https://test.max.ru/file', target, limit)

    def test_stream_limit_removes_partial_file(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'download.pdf'
            with self.assertRaisesRegex(ValueError, 'size limit'):
                self.download(path, Response(b'%PDF-' + b'x' * 32))
            self.assertFalse(path.exists())

    def test_invalid_header_removes_file(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'download.pdf'
            with self.assertRaisesRegex(ValueError, 'Not a PDF'):
                self.download(path, Response(b'fictional bytes'))
            self.assertFalse(path.exists())

    def test_read_failure_removes_partial_file(self):
        class Broken(Response):
            def read(self, size=-1):
                if self.tell():
                    raise OSError('synthetic connection loss')
                return super().read(5)
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'download.pdf'
            with self.assertRaisesRegex(OSError, 'synthetic'):
                self.download(path, Broken(b'%PDF-synthetic'))
            self.assertFalse(path.exists())

    def test_existing_file_is_preserved(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'download.pdf'
            path.write_bytes(b'existing fictional data')
            with self.assertRaises(FileExistsError):
                self.download(path, Response(b'%PDF-new'))
            self.assertEqual(path.read_bytes(), b'existing fictional data')

    def test_success_and_header_limit(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'download.pdf'
            self.download(path, Response(b'%PDF-synthetic'))
            self.assertEqual(path.read_bytes(), b'%PDF-synthetic')
            other = Path(folder) / 'oversized.pdf'
            with self.assertRaisesRegex(ValueError, 'size limit'):
                self.download(other, Response(b'', {'Content-Length': '17'}))
            self.assertFalse(other.exists())
