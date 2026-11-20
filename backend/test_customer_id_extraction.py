import unittest

from main import extract_dialogflow_customer_id


class CustomerIdExtractionTests(unittest.TestCase):
    def test_extracts_natural_language_customer_ids_for_all_locales(self):
        requests = (
            {
                "languageCode": "en",
                "text": "My customer ID is CUST1001",
            },
            {
                "languageCode": "hi",
                "text": "मेरा ग्राहक आईडी CUST1001 है",
            },
            {
                "languageCode": "ar",
                "text": "رقم العميل CUST1001",
            },
        )

        for request in requests:
            with self.subTest(language=request["languageCode"]):
                self.assertEqual(
                    extract_dialogflow_customer_id(request),
                    "CUST1001",
                )

    def test_extracts_full_utterance_from_nested_alternate_intent_parameter(self):
        request = {
            "intentInfo": {
                "parameters": {
                    "customerId": {
                        "original": {
                            "text": "My customer ID is CUST-1001",
                        },
                        "resolved": "cust1001",
                    },
                },
            },
        }

        self.assertEqual(
            extract_dialogflow_customer_id(request),
            "CUST1001",
        )

    def test_extracts_customer_id_from_fulfillment_and_form_parameters(self):
        fulfillment_request = {
            "fulfillmentInfo": {
                "parameters": {
                    "customer_id": {
                        "string": "ग्राहक आईडी CUST_1001",
                    },
                },
            },
        }
        form_request = {
            "pageInfo": {
                "formInfo": {
                    "parameterInfo": [
                        {
                            "displayName": "customerId",
                            "value": {
                                "original": "رقم العميل CUST1001",
                            },
                        },
                    ],
                },
            },
        }

        self.assertEqual(
            extract_dialogflow_customer_id(fulfillment_request),
            "CUST1001",
        )
        self.assertEqual(
            extract_dialogflow_customer_id(form_request),
            "CUST1001",
        )

    def test_uses_session_parameter_and_transcript(self):
        request = {
            "sessionInfo": {
                "parameters": {
                    "customer_id": "CUST1001",
                },
            },
            "transcript": "customer ID CUST1001",
        }

        self.assertEqual(
            extract_dialogflow_customer_id(request),
            "CUST1001",
        )

    def test_does_not_extract_invoice_amount_or_date_as_customer_id(self):
        request = {
            "sessionInfo": {
                "parameters": {
                    "invoice_number": "INV-1001",
                    "amount": 1001,
                    "expected_payment_date": "2026-11-30",
                },
            },
            "text": "Please check invoice number CUST1001",
        }

        self.assertIsNone(extract_dialogflow_customer_id(request))

    def test_rejects_missing_malformed_or_conflicting_customer_ids(self):
        self.assertIsNone(extract_dialogflow_customer_id({}))
        self.assertIsNone(
            extract_dialogflow_customer_id({
                "sessionInfo": {
                    "parameters": {
                        "customer_id": "customer 1001",
                    },
                },
            })
        )
        self.assertIsNone(
            extract_dialogflow_customer_id({
                "sessionInfo": {
                    "parameters": {
                        "customer_id": "CUST1001",
                    },
                },
                "text": "My customer ID is CUST2002",
            })
        )


if __name__ == "__main__":
    unittest.main()
