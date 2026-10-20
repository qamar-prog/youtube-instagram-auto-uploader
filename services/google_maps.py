"""Geoapify helpers used to enrich lead addresses."""

import os
from typing import Any

import requests
from dotenv import load_dotenv

load_dotenv()

GEOCODING_URL = "https://api.geoapify.com/v1/geocode/search"


def geocode_address(address: str) -> dict[str, Any]:
    """Return Geoapify's first geocoding result for *address*.

    Raises:
        ValueError: if the API key or address is missing, or Geoapify finds no
            matching address.
        requests.HTTPError: if the geocoding request fails at HTTP level.
        requests.RequestException: for network errors.
    """
    api_key = os.getenv("GEOAPIFY_API_KEY", "").strip()
    if not api_key:
        raise ValueError("GEOAPIFY_API_KEY is not configured")

    address = address.strip()
    if not address:
        raise ValueError("address must not be empty")

    response = requests.get(
        GEOCODING_URL,
        params={"text": address, "apiKey": api_key, "format": "json"},
        timeout=15,
    )
    response.raise_for_status()
    payload = response.json()

    if not payload.get("results"):
        error_message = payload.get("message", "no matching result")
        raise ValueError(f"Geoapify geocoding failed: {error_message}")

    return payload["results"][0]