from app import ensure_settings_store


if __name__ == "__main__":
    ensure_settings_store()
    print("SQLite migration completed without clearing existing mistake data.")
