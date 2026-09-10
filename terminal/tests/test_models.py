from __future__ import annotations

import unittest

from terminal.models import AttendanceEvent, AuthenticationResult


class ModelTests(unittest.TestCase):
    def test_face_payload_contains_confidence(self) -> None:
        result = AuthenticationResult("1234567890", "face", 0.81)
        event = AttendanceEvent.from_authentication(
            result, device_id="gate-1", event_type="check_in"
        )
        self.assertEqual(event.payload()["confidence"], 0.81)
        self.assertEqual(event.payload()["eventType"], "check_in")

    def test_card_payload_does_not_expose_confidence_or_idm(self) -> None:
        result = AuthenticationResult("1234567890", "card")
        event = AttendanceEvent.from_authentication(
            result, device_id="gate-1", event_type="check_out"
        )
        self.assertNotIn("confidence", event.payload())
        self.assertNotIn("idm", event.payload())

    def test_student_number_must_be_ascii_digits(self) -> None:
        with self.assertRaises(ValueError):
            AuthenticationResult("１２３４５６７８９０", "card")


if __name__ == "__main__":
    unittest.main()

