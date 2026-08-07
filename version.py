from pathlib import Path

APP_VERSION: str = (Path(__file__).parent / "VERSION").read_text().strip()
