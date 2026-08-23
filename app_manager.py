import subprocess
import json
import os
from rapidfuzz import process, fuzz

DATABASE_FILE = "app_database.json"


# --------------------------------------------------
# Load / Save database
# --------------------------------------------------

def load_database():
    if not os.path.exists(DATABASE_FILE):
        return {}

    try:
        with open(DATABASE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {}


def save_database(db):
    with open(DATABASE_FILE, "w", encoding="utf-8") as f:
        json.dump(db, f, indent=4)


# --------------------------------------------------
# Get installed apps from Windows
# --------------------------------------------------

def get_start_apps():

    cmd = [
        "powershell",
        "-NoProfile",
        "-Command",
        "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json"
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8"
    )

    if result.returncode != 0:
        print(result.stderr)
        return []

    data = json.loads(result.stdout)

    if isinstance(data, dict):
        data = [data]

    return data


# --------------------------------------------------
# Interactive setup
# --------------------------------------------------

def add_app():

    apps = get_start_apps()

    while True:

        query = input("\nApp name (exit to quit): ").strip()

        if query.lower() == "exit":
            break

        names = [a["Name"] for a in apps]

        matches = process.extract(
            query,
            names,
            scorer=fuzz.WRatio,
            limit=10
        )

        if not matches:
            print("No apps found.")
            continue

        print()

        for i, m in enumerate(matches, 1):
            print(f"{i}. {m[0]}")

        choice = input("\nChoose number: ")

        try:

            chosen = matches[int(choice)-1][0]

            app = next(a for a in apps if a["Name"] == chosen)

            db = load_database()

            db[query.lower()] = {
                "name": app["Name"],
                "appid": app["AppID"]
            }

            save_database(db)

            print(f"\nSaved '{query}' -> {app['Name']}")

        except Exception:
            print("Invalid choice.")


if __name__ == "__main__":
    add_app()