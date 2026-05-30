import os

from app import APP_URL, run_daily_schedule


if __name__ == "__main__":
    result = run_daily_schedule(app_url=os.environ.get("APP_URL", APP_URL))
    print(result["message"])
