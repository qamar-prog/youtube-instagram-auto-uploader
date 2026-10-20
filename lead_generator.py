"""Find business leads in a selected country and city using Geoapify."""

import argparse
import json
import os
from typing import Any

import requests
from dotenv import load_dotenv

from services.google_maps import geocode_address

load_dotenv()

PLACES_URL = "https://api.geoapify.com/v2/places"
DEFAULT_CATEGORY = "commercial"
DEFAULT_RADIUS_METERS = 10000


def find_leads(
    country: str,
    city: str,
    category: str = DEFAULT_CATEGORY,
    limit: int = 20,
    radius_meters: int = DEFAULT_RADIUS_METERS,
) -> list[dict[str, Any]]:
    """Find places near *city* in *country* and return lead records."""
    api_key = os.getenv("GEOAPIFY_API_KEY", "").strip()
    if not api_key:
        raise ValueError("GEOAPIFY_API_KEY is not configured")
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    if radius_meters < 1:
        raise ValueError("radius_meters must be positive")

    location = geocode_address(f"{city}, {country}")
    latitude = location.get("lat")
    longitude = location.get("lon")
    if latitude is None or longitude is None:
        raise ValueError("Geoapify did not return coordinates for the city")

    response = requests.get(
        PLACES_URL,
        params={
            "categories": category,
            "filter": f"circle:{longitude},{latitude},{radius_meters}",
            "limit": limit,
            "apiKey": api_key,
        },
        timeout=20,
    )
    response.raise_for_status()

    leads = []
    for feature in response.json().get("features", []):
        properties = feature.get("properties", {})
        leads.append({
            "name": properties.get("name", "Unnamed business"),
            "address": properties.get("formatted", ""),
            "phone": properties.get("contact", {}).get("phone")
            or properties.get("datasource", {}).get("raw", {}).get("phone"),
            "website": properties.get("website"),
            "latitude": properties.get("lat"),
            "longitude": properties.get("lon"),
            "categories": properties.get("categories", []),
        })
    return leads


def main() -> None:
    parser = argparse.ArgumentParser(description="Find business leads by country and city.")
    parser.add_argument("--country", required=True, help="Country name")
    parser.add_argument("--city", required=True, help="City name")
    parser.add_argument("--category", default=DEFAULT_CATEGORY, help="Geoapify category")
    parser.add_argument("--limit", type=int, default=20, help="Maximum leads to return")
    parser.add_argument("--radius", type=int, default=DEFAULT_RADIUS_METERS, help="Search radius in meters")
    parser.add_argument("--output", default="leads.json", help="Output JSON file")
    args = parser.parse_args()

    leads = find_leads(args.country, args.city, args.category, args.limit, args.radius)
    with open(args.output, "w", encoding="utf-8") as file:
        json.dump(leads, file, indent=2)
    print(f"Found {len(leads)} leads. Saved to {args.output}")
    for lead in leads:
        print(f"- {lead['name']}: {lead['address']}")


if __name__ == "__main__":
    main()