import os

from app import APP_URL, generate_daily_material


if __name__ == "__main__":
    result = generate_daily_material(app_url=os.environ.get("APP_URL", APP_URL))
    print(result["message"])
