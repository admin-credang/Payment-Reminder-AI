import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from google.auth.exceptions import DefaultCredentialsError
from fastapi import HTTPException

import main


class DialogflowCredentialsTests(unittest.TestCase):
    def test_builds_impersonated_credentials_from_vercel_oidc_token(self):
        source_credentials = object()
        credentials = object()
        oidc_token = "test-oidc-token"
        with (
            patch(
                "main.identity_pool.Credentials",
                return_value=source_credentials,
            ) as identity_pool_credentials,
            patch(
                "main.impersonated_credentials.Credentials",
                return_value=credentials,
            ) as impersonated,
            patch("main.dialogflowcx.SessionsClient") as sessions_client,
        ):
            client = main.get_dialogflow_client(oidc_token)

        identity_pool_credentials.assert_called_once()
        identity_pool_arguments = identity_pool_credentials.call_args.kwargs
        self.assertEqual(
            identity_pool_arguments["audience"],
            main.VERCEL_WIF_AUDIENCE,
        )
        self.assertEqual(
            identity_pool_arguments["subject_token_type"],
            "urn:ietf:params:oauth:token-type:jwt",
        )
        self.assertEqual(
            identity_pool_arguments["scopes"],
            [main.GOOGLE_CLOUD_PLATFORM_SCOPE],
        )
        self.assertEqual(
            identity_pool_arguments["subject_token_supplier"].get_subject_token(
                None,
                None,
            ),
            oidc_token,
        )
        impersonated.assert_called_once_with(
            source_credentials=source_credentials,
            target_principal=main.DIALOGFLOW_SERVICE_ACCOUNT,
            target_scopes=[main.GOOGLE_CLOUD_PLATFORM_SCOPE],
        )
        sessions_client.assert_called_once_with(
            credentials=credentials,
            client_options={
                "api_endpoint": (
                    f"{main.DIALOGFLOW_LOCATION}-dialogflow.googleapis.com"
                )
            },
        )
        self.assertIs(client, sessions_client.return_value)

    def test_falls_back_to_application_default_credentials_outside_vercel(self):
        credentials = object()
        with (
            patch.dict(os.environ, {"VERCEL": ""}),
            patch(
                "main.google_auth_default",
                return_value=(credentials, "test-project"),
            ) as google_auth_default,
            patch("main.dialogflowcx.SessionsClient") as sessions_client,
        ):
            main.get_dialogflow_client()

        google_auth_default.assert_called_once_with(
            scopes=[main.GOOGLE_CLOUD_PLATFORM_SCOPE]
        )
        self.assertIs(
            sessions_client.call_args.kwargs["credentials"],
            credentials,
        )

    def test_missing_vercel_token_returns_clear_server_error(self):
        with (
            patch.dict(os.environ, {"VERCEL": "1"}),
            patch("main.google_auth_default") as google_auth_default,
        ):
            with self.assertRaises(HTTPException) as error:
                main.get_dialogflow_client()

        google_auth_default.assert_not_called()
        self.assertEqual(error.exception.status_code, 500)
        self.assertEqual(
            error.exception.detail,
            "Google Cloud credentials are unavailable or invalid",
        )

    def test_tts_session_uses_request_scoped_credentials(self):
        credentials = object()
        oidc_token = "tts-request-oidc-token"
        with (
            patch(
                "main.get_google_credentials",
                return_value=credentials,
            ) as get_credentials,
            patch("main.AuthorizedSession") as authorized_session,
        ):
            session = main.create_google_tts_session(oidc_token)

        get_credentials.assert_called_once_with(oidc_token)
        authorized_session.assert_called_once_with(credentials)
        self.assertIs(session, authorized_session.return_value)

    def test_chat_passes_request_token_to_dialogflow_and_tts(self):
        query_result = SimpleNamespace(
            response_messages=[],
            current_page=SimpleNamespace(display_name="Start Page"),
            match=None,
        )
        client = SimpleNamespace(
            session_path=lambda *args: "test-session-path",
            detect_intent=lambda **kwargs: SimpleNamespace(
                query_result=query_result
            ),
        )
        oidc_token = "request-oidc-token"
        with (
            patch("main.get_dialogflow_client", return_value=client) as get_client,
            patch(
                "main.synthesize_arabic_tts",
                return_value="audio",
            ) as synthesize,
        ):
            main.chat_with_dialogflow(
                main.ChatRequest(message="Hello", language_code="ar"),
                SimpleNamespace(headers={"x-vercel-oidc-token": oidc_token}),
            )

        get_client.assert_called_once_with(oidc_token)
        synthesize.assert_called_once_with("", oidc_token)

    def test_chat_returns_clear_error_when_local_credentials_are_missing(self):
        request = main.ChatRequest(message="Hello")
        http_request = SimpleNamespace(headers={})
        with (
            patch.dict(os.environ, {"VERCEL": ""}),
            patch(
                "main.google_auth_default",
                side_effect=DefaultCredentialsError("credentials not found"),
            ),
        ):
            with self.assertRaises(HTTPException) as error:
                main.chat_with_dialogflow(request, http_request)

        self.assertEqual(error.exception.status_code, 500)
        self.assertEqual(
            error.exception.detail,
            "Google Cloud credentials are unavailable or invalid",
        )


if __name__ == "__main__":
    unittest.main()
