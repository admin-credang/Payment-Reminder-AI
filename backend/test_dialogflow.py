import uuid
from google.cloud import dialogflowcx_v3 as cx

PROJECT_ID = "payment-reminder-ai-2026"
LOCATION = "asia-south1"
AGENT_ID = "12d88d77-8d49-4d31-8d41-818248d455bb"

client = cx.SessionsClient(
    client_options={
        "api_endpoint": f"{LOCATION}-dialogflow.googleapis.com"
    }
)

session_id = str(uuid.uuid4())
session_path = client.session_path(
    PROJECT_ID, LOCATION, AGENT_ID, session_id
)

query_input = cx.QueryInput(
    text=cx.TextInput(text="Hello"),
    language_code="en"
)

request = cx.DetectIntentRequest(
    session=session_path,
    query_input=query_input
)

response = client.detect_intent(request=request)

print("Dialogflow connection successful!")
print("Agent response:")

for message in response.query_result.response_messages:
    if message.text and message.text.text:
        for text in message.text.text:
            print("-", text)
