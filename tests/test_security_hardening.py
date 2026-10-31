import os
import tempfile
import unittest
from pathlib import Path

from app.services.secret_storage import open_secret, seal
from utils.path_security import safe_upload_path


class SecretStorageTests(unittest.TestCase):
    def test_authenticated_encryption_and_context_binding(self):
        with tempfile.TemporaryDirectory() as folder:
            previous = os.environ.get('BYGOOFISH_MASTER_KEY_FILE')
            os.environ['BYGOOFISH_MASTER_KEY_FILE'] = str(Path(folder) / 'master.key')
            try:
                encrypted = seal('fixture-secret', 'cookie:fixture')
                self.assertTrue(encrypted.startswith('enc:v1:'))
                self.assertEqual(open_secret(encrypted, 'cookie:fixture'), 'fixture-secret')
                with self.assertRaises(RuntimeError):
                    open_secret(encrypted, 'cookie:other')
                self.assertEqual((Path(folder) / 'master.key').stat().st_mode & 0o777, 0o600)
            finally:
                if previous is None:
                    os.environ.pop('BYGOOFISH_MASTER_KEY_FILE', None)
                else:
                    os.environ['BYGOOFISH_MASTER_KEY_FILE'] = previous


class UploadPathTests(unittest.TestCase):
    def test_upload_path_rejects_escape_and_symlink(self):
        with tempfile.TemporaryDirectory() as folder:
            project = Path(folder)
            upload = project / 'static/uploads/images'
            upload.mkdir(parents=True)
            outside = project / 'outside.txt'
            outside.write_text('fixture')
            (upload / 'link.png').symlink_to(outside)
            with self.assertRaises(ValueError):
                safe_upload_path('static/uploads/images/../../outside.txt', project)
            with self.assertRaises(ValueError):
                safe_upload_path('static/uploads/images/link.png', project)
            (upload / 'ok.png').write_bytes(b'fixture')
            self.assertEqual(safe_upload_path('static/uploads/images/ok.png', project), upload / 'ok.png')


if __name__ == '__main__':
    unittest.main()
