"""Voice transcode arc (#97873): [[audio_as_voice]] works for every audio format.

Found by a maintainer-directed all-platform hint verification: the telegram
hint claimed '.ogg sends as voice bubbles' but bare MEDIA:.ogg (no directive)
ships as a document, and mp3+directive DEAD-ENDED into document delivery.
Fix: shared transcode_to_ogg_opus in base.py; telegram/feishu send_voice
transcode non-opus input; should_send_media_as_audio honors explicit
is_voice for any audio ext on telegram; is_voice threaded through the three
dispatch call sites.
"""
import asyncio
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from gateway.platforms.base import should_send_media_as_audio, transcode_to_ogg_opus


class TestVoiceRouting(unittest.TestCase):


    def test_other_platforms_unchanged(self):
        self.assertTrue(should_send_media_as_audio("feishu", ".mp3", is_voice=False))
        self.assertFalse(should_send_media_as_audio("feishu", ".qzx7", is_voice=False))


class TestTranscodeEngine(unittest.TestCase):
    def test_missing_ffmpeg_returns_none(self):
        with patch("shutil.which", return_value=None):
            self.assertIsNone(transcode_to_ogg_opus("/tmp/x.mp3"))

    def test_transcode_failure_cleans_up(self):
        """A failing ffmpeg leaves no orphan temp file behind."""
        import subprocess

        fake = subprocess.CompletedProcess(args=[], returncode=1, stdout=b"", stderr=b"boom")
        with patch("shutil.which", return_value="ffmpeg"), \
             patch("subprocess.run", return_value=fake):
            before = set(os.listdir(tempfile.gettempdir()))
            self.assertIsNone(transcode_to_ogg_opus("/tmp/x.mp3"))
            leaked = [f for f in os.listdir(tempfile.gettempdir())
                      if f.startswith("voice_transcode_") and f not in before]
            self.assertEqual(leaked, [])




if __name__ == "__main__":
    unittest.main()
