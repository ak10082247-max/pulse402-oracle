"""Merchant configuration and public payment-contract regressions; no network."""
import base64
import json
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from engine import payments
import app


class ConfigurationTests(unittest.TestCase):
    def test_retired_environment_recipients_rotate(self):
        with patch.dict(os.environ, {"PAYOUT_WALLET_ADDRESS": "  0x73279FA4BADA7CAC888C62CDA4F5C8104765F6F1 ", "SOLANA_MERCHANT_ADDRESS": " 3tQ4GthjUMqNsyrb1rZMocMHoNewFQWjDUvHZqWeYuCN "}):
            base, sol = payments.requirements()
        self.assertEqual(base["payTo"], "0x80E0Ae84679b7f11bE171DaADd492FBECf628cd6")
        self.assertEqual(sol["payTo"], "9wuMysgXwNhtQeqJgGByAiMYc4EjzZ5wUSmnjrrt3ffh")

    def test_custom_recipient_is_preserved(self):
        with patch.dict(os.environ, {"PAYOUT_WALLET_ADDRESS": "0x" + "12" * 20}):
            self.assertEqual(payments.requirements()[0]["payTo"], "0x" + "12" * 20)

    def test_malformed_solana_configuration_is_structured(self):
        with patch.dict(os.environ, {"SOLANA_MERCHANT_ADDRESS": "O0invalid"}):
            with self.assertRaises(payments.PaymentError) as result:
                payments.requirements()
        self.assertEqual(result.exception.status, 503)

    def test_nonfinite_payment_header_is_rejected(self):
        raw = base64.b64encode(b'{"x402Version": NaN}').decode()
        with self.assertRaises(payments.PaymentError) as result:
            payments.payment_header({"PAYMENT-SIGNATURE": raw})
        self.assertEqual(result.exception.code, "INVALID_PAYMENT_HEADER")

    def test_openapi_describes_actual_filters_and_credit_choice(self):
        schema = app.app.openapi()
        route = schema["paths"]["/v1/pulse/arb-routes"]["get"]
        self.assertEqual(route["parameters"][0]["name"], "min_spread_bps")
        self.assertIn({"pulseSession": []}, route["security"])
        self.assertIn("402", route["responses"])
        self.assertIn("503", route["responses"])
        prepay = schema["paths"]["/v1/pass/prepay"]["post"]
        self.assertEqual(prepay["security"], [{"x402PaymentSignature": []}])


if __name__ == "__main__":
    unittest.main()
