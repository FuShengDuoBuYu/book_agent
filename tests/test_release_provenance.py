import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from app.core.config import Settings, get_settings


class ReleaseProvenanceTests(unittest.TestCase):
    def tearDown(self):
        get_settings.cache_clear()

    def test_release_sha_is_optional_for_local_development(self):
        self.assertIsNone(Settings().release_revision)

    def test_deployed_release_uses_full_git_sha(self):
        revision = 'a' * 40
        with patch.dict(os.environ, {'AGENT_RELEASE_REVISION': revision}, clear=True):
            get_settings.cache_clear()
            self.assertEqual(get_settings().release_revision, revision)
        with self.assertRaises(ValidationError):
            Settings(release_revision='short-or-untrusted')
