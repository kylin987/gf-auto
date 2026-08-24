from __future__ import annotations

import os
from pathlib import Path
import shutil
from urllib.parse import unquote
import json


APP_DATA_DIR_NAME = "yhs-fish-plugin"


def app_data_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    target = Path(root) / APP_DATA_DIR_NAME
    target.mkdir(parents=True, exist_ok=True)
    return target


def default_cookie_file() -> str:
    target = app_data_dir() / "cookies.json"
    legacy = Path.cwd() / "cookies.json"
    if not target.exists() and legacy.is_file() and legacy.resolve() != target.resolve():
        try:
            shutil.copy2(legacy, target)
        except OSError:
            pass
    return str(target)


def default_log_dir() -> str:
    return str(app_data_dir() / "log")


def saved_chrome_account() -> dict[str, str]:
    try:
        payload = json.loads(Path(default_cookie_file()).read_text(encoding="utf-8"))
        cookies = payload.get("cookies", payload) if isinstance(payload, dict) else {}
        if not isinstance(cookies, dict):
            return {}
        return {
            "nick": unquote(str(cookies.get("tracknick") or "")).strip(),
            "userId": str(cookies.get("unb") or "").strip(),
        }
    except (OSError, ValueError, TypeError):
        return {}
