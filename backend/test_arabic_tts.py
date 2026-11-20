import base64
import json
import unittest
from unittest.mock import patch

import main


class FakeResponse:
    def __init__(self, data, status_code=200):
        self.data = data
        self.status_code = status_code
        self.ok = status_code < 400

    def json(self):
        return self.data


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.request = None

    def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return self.response

    def get(self, url, **kwargs):
        self.request = (url, kwargs)
        return self.response


class ArabicTtsTests(unittest.TestCase):
    def tearDown(self):
        main.get_arabic_tts_voice_name.cache_clear()

    def test_prefers_chirp_then_wavenet_for_ar_xa(self):
        voices = {
            "voices": [
                {
                    "name": "ar-XA-Standard-A",
                    "languageCodes": ["ar-XA"],
                },
                {
                    "name": "ar-XA-Wavenet-A",
                    "languageCodes": ["ar-XA"],
                },
                {
                    "name": "ar-XA-Chirp3-HD-Achernar",
                    "languageCodes": ["ar-XA"],
                },
                {
                    "name": "en-US-Chirp3-HD-Achernar",
                    "languageCodes": ["en-US"],
                },
            ],
        }
        with (
            patch("main.create_google_tts_session"),
            patch("main.google_tts_json_request", return_value=voices),
        ):
            self.assertEqual(
                main.get_arabic_tts_voice_name(),
                "ar-XA-Chirp3-HD-Achernar",
            )

        main.get_arabic_tts_voice_name.cache_clear()
        voices["voices"] = voices["voices"][:2]
        with (
            patch("main.create_google_tts_session"),
            patch("main.google_tts_json_request", return_value=voices),
        ):
            self.assertEqual(
                main.get_arabic_tts_voice_name(),
                "ar-XA-Wavenet-A",
            )

    def test_synthesizes_exact_arabic_text_as_mp3_over_utf8_json(self):
        arabic_text = "شكرًا لك. رقم الفاتورة 12345."
        audio_content = base64.b64encode(b"fake-mp3-data").decode("ascii")
        session = FakeSession(FakeResponse({"audioContent": audio_content}))
        with (
            patch("main.create_google_tts_session", return_value=session),
            patch(
                "main.get_arabic_tts_voice_name",
                return_value="ar-XA-Chirp3-HD-Achernar",
            ),
        ):
            result = main.synthesize_arabic_tts(arabic_text)

        url, request = session.request
        payload = json.loads(request["data"].decode("utf-8"))
        self.assertEqual(result, audio_content)
        self.assertTrue(url.endswith("/text:synthesize"))
        self.assertEqual(payload["input"]["text"], arabic_text)
        self.assertEqual(payload["voice"]["languageCode"], "ar-XA")
        self.assertEqual(
            payload["voice"]["name"],
            "ar-XA-Chirp3-HD-Achernar",
        )
        self.assertEqual(payload["audioConfig"]["audioEncoding"], "MP3")

    def test_does_not_select_an_english_voice_for_arabic(self):
        voices = {
            "voices": [
                {
                    "name": "en-US-Standard-A",
                    "languageCodes": ["en-US"],
                },
            ],
        }
        with (
            patch("main.create_google_tts_session"),
            patch("main.google_tts_json_request", return_value=voices),
        ):
            with self.assertRaises(main.GoogleTTSUnavailableError) as error:
                main.get_arabic_tts_voice_name()

        self.assertEqual(error.exception.reason, "no_supported_arabic_voice")

    def test_preserves_google_service_disabled_reason(self):
        session = FakeSession(FakeResponse({
            "error": {
                "status": "PERMISSION_DENIED",
                "details": [
                    {
                        "reason": "SERVICE_DISABLED",
                    },
                ],
            },
        }, status_code=403))
        with self.assertRaises(main.GoogleTTSUnavailableError) as error:
            main.google_tts_json_request(
                session,
                "GET",
                "https://texttospeech.googleapis.com/v1/voices",
            )

        self.assertEqual(error.exception.status_code, 403)
        self.assertEqual(error.exception.reason, "SERVICE_DISABLED")


if __name__ == "__main__":
    unittest.main()
