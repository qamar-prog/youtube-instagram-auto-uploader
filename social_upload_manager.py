import argparse
import os

from dotenv import load_dotenv

from services.instagram_selenium import upload_instagram
from services.facebook_selenium import upload_facebook

load_dotenv()


def main():
    parser = argparse.ArgumentParser(
        description="Prepare/publish a video to Facebook and/or Instagram using Selenium."
    )
    parser.add_argument("--video", required=True, help="Local path to an MP4 video")
    parser.add_argument("--caption", required=True, help="Caption text")
    parser.add_argument(
        "--destinations", nargs="+", choices=["facebook", "instagram"],
        required=True, help="One or both destinations"
    )
    parser.add_argument(
        "--facebook-page-url",
        default=os.getenv("FACEBOOK_PAGE_URL", ""),
        help="Full URL of the Facebook Page you manage"
    )
    parser.add_argument(
        "--publish", action="store_true",
        help="Actually click Share/Post. Without this flag, run in preview mode."
    )
    args = parser.parse_args()

    results = {}
    for destination in args.destinations:
        try:
            if destination == "instagram":
                results[destination] = upload_instagram(
                    args.video, args.caption, publish=args.publish
                )
            elif destination == "facebook":
                results[destination] = upload_facebook(
                    args.video, args.caption,
                    page_url=args.facebook_page_url,
                    publish=args.publish,
                )
        except Exception as exc:
            results[destination] = f"FAILED: {exc}"
            print(f"{destination} failed: {exc}")

    print("\nResults:")
    for destination, result in results.items():
        print(f"  {destination}: {result}")


if __name__ == "__main__":
    main()
