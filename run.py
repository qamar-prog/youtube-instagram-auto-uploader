"""Menu for lead search, publishing, and Telegram bot control."""

import subprocess
import sys


def main() -> None:
    print("1. Find business leads")
    print("2. Publish next video")
    print("3. Publish due YouTube videos")
    print("4. Start Telegram control bot")
    choice = input("Choose an option: ").strip()

    if choice == "1":
        country = input("Country: ").strip()
        city = input("City: ").strip()
        category = input("Business category [commercial]: ").strip() or "commercial"
        subprocess.run(
            [sys.executable, "lead_generator.py", "--country", country, "--city", city, "--category", category],
            check=True,
        )
    elif choice == "2":
        print("Destinations: youtube, telegram, both")
        destination = input("Destination [youtube]: ").strip().lower() or "youtube"
        if destination not in {"youtube", "telegram", "both"}:
            raise SystemExit("Destination must be youtube, telegram, or both.")
        command = [
            sys.executable,
            "upload_unlisted.py",
            "--destination",
            destination,
        ]
        if destination in {"youtube", "both"}:
            slot = input("YouTube slot (A or B): ").strip().upper()
            if slot not in {"A", "B"}:
                raise SystemExit("YouTube slot must be A or B.")
            command.extend(["--slot", slot])
        subprocess.run(command, check=True)
    elif choice == "3":
        subprocess.run([sys.executable, "publish_scheduled.py"], check=True)
    elif choice == "4":
        subprocess.run([sys.executable, "telegram_bot.py"], check=True)
    else:
        raise SystemExit("Choose 1, 2, 3, or 4.")


if __name__ == "__main__":
    main()