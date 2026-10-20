import unittest
from unittest.mock import Mock, patch

import requests

from utils import _rate_limit_delay, retry_on_transient


class RetryAfterTests(unittest.TestCase):
    def test_retry_after_seconds_override_short_exponential_backoff(self):
        response = Mock(
            status_code=429,
            headers={"Retry-After": "90"},
        )
        error = requests.HTTPError("rate limited", response=response)
        attempts = 0

        @retry_on_transient(max_attempts=2, base_delay=2)
        def request():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise error
            return "ok"

        with patch("utils.time.sleep") as sleep:
            result = request()

        self.assertEqual(result, "ok")
        sleep.assert_called_once_with(90)

    def test_long_retry_after_propagates_for_configured_provider_fallback(self):
        response = Mock(
            status_code=429,
            headers={"Retry-After": "600"},
        )
        error = requests.HTTPError("rate limited", response=response)

        @retry_on_transient(
            max_attempts=4,
            base_delay=2,
            max_retry_delay=60,
        )
        def request():
            raise error

        with patch("utils.time.sleep") as sleep:
            with self.assertRaises(requests.HTTPError):
                request()

        sleep.assert_not_called()

    def test_rate_limit_reset_headers_accept_duration_text(self):
        response = Mock(
            status_code=429,
            headers={
                "x-ratelimit-reset-requests": "1m30s",
                "x-ratelimit-reset-tokens": "45s",
            },
        )
        error = requests.HTTPError("rate limited", response=response)

        self.assertEqual(_rate_limit_delay(error), 90)

    def test_rate_limit_error_body_reset_is_parsed(self):
        response = Mock(
            status_code=429,
            headers={},
        )
        response.json.return_value = {
            "error": {"message": "Please try again in 2m 15.5s"}
        }
        error = requests.HTTPError("rate limited", response=response)

        self.assertEqual(_rate_limit_delay(error), 135.5)

    def test_retry_after_http_date_is_converted_to_wait_duration(self):
        response = Mock(
            status_code=429,
            headers={"Retry-After": "Wed, 21 Oct 2037 07:28:00 GMT"},
        )
        error = requests.HTTPError("rate limited", response=response)

        self.assertGreater(_rate_limit_delay(error), 0)

    def test_retry_after_duration_text_is_respected(self):
        response = Mock(
            status_code=429,
            headers={"Retry-After": "2m 10s"},
        )
        error = requests.HTTPError("rate limited", response=response)

        self.assertEqual(_rate_limit_delay(error), 130)


if __name__ == "__main__":
    unittest.main()
