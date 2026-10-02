import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Termux-пути
HOME = Path(os.path.expanduser("~"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
CODEX_PATH = os.getenv("CODEX_PATH", "codex")
ANDROID_PRESET = os.getenv("ANDROID_PRESET", "Android")

# Путь к проекту Godot (внутри Termux или на /sdcard)
DEFAULT_PROJECT_PATH = os.getenv("PROJECT_PATH", str(HOME / "project"))

CONFIG_FILE = Path(__file__).parent / "studio_config.json"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            import json
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "current_project": "default",
        "projects": {"default": DEFAULT_PROJECT_PATH},
        "model": "gpt-5.6-luna",
        "effort": "high",
    }


def save_config(cfg: dict) -> None:
    import json
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


CONFIG = load_config()

_required = {
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "TELEGRAM_CHAT_ID": TELEGRAM_CHAT_ID,
}
for _key, _value in _required.items():
    if not _value:
        raise ValueError(f"Не задано в .env: {_key}")

# PROJECT_PATH вычисляется динамически
PROJECT_PATH = Path(
    CONFIG["projects"].get(
        CONFIG.get("current_project", "default"),
        DEFAULT_PROJECT_PATH,
    )
)