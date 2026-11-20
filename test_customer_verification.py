import uuid
from google.cloud import dialogflowcx_v3 as cx

PROJECT = "payment-reminder-ai-2026"
LOCATION = "asia-south1"
AGENT = "12d88d77-8d49-4d31-8d41-818248d455bb"

client = cx.SessionsClient(
    client_options={
        "api_endpoint": f"{LOCATION}-dialogflow.googleapis.com"
    }
)

session_id = str(uuid.uuid4())
session = client.session_path(PROJECT, LOCATION, AGENT, session_id)

def send_message(message):
    request = cx.DetectIntentRequest(
        session=session,
        query_input=cx.QueryInput(
            text=cx.TextInput(text=message),
            language_code="en"
        )
    )

    response = client.detect_intent(request=request)
    result = response.query_result

    print("\nUSER:", message)
    print("PAGE:", result.current_page.display_name)
    print("INTENT:", result.match.intent.display_name if result.match and result.match.intent else None)
    print("PARAMETERS:", dict(result.parameters) if result.parameters else {})
    print("RESPONSES:")

    for msg in result.response_messages:
        if msg.text:
            for text in msg.text.text:
                print("-", text)

send_message("Hello")
send_message("CUST1001")
