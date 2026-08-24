from __future__ import annotations

import os
from pathlib import Path
import shutil
from urllib.parse import unquote
import json
import uuid


APP_DATA_DIR_NAME = "yhs-fish-plugin"
INSTANCES_FILE_NAME = "instances.json"


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


def instances_file() -> Path:
    return app_data_dir() / INSTANCES_FILE_NAME


def instance_dir(instance_id: str) -> Path:
    target = app_data_dir() / "instances" / str(instance_id)
    target.mkdir(parents=True, exist_ok=True)
    return target


def instance_cookie_file(instance_id: str) -> str:
    return str(instance_dir(instance_id) / "cookies.json")


def instance_log_dir(instance_id: str) -> str:
    target = instance_dir(instance_id) / "log"
    target.mkdir(parents=True, exist_ok=True)
    return str(target)


def instance_chrome_profile_dir(instance_id: str) -> str:
    target = instance_dir(instance_id) / "chrome-profile"
    target.mkdir(parents=True, exist_ok=True)
    return str(target)


def load_instances() -> list[dict]:
    """读取店铺实例，并将历史单店 Cookie 无感迁移为默认实例。"""
    path = instances_file()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        instances = payload.get("instances", []) if isinstance(payload, dict) else []
        if isinstance(instances, list):
            return [item for item in instances if isinstance(item, dict) and item.get("id")]
    except (OSError, ValueError, TypeError):
        pass

    legacy_cookie = Path(default_cookie_file())
    if not legacy_cookie.is_file():
        return []
    instance_id = "legacy-" + uuid.uuid4().hex[:12]
    cookie_file = Path(instance_cookie_file(instance_id))
    try:
        shutil.copy2(legacy_cookie, cookie_file)
    except OSError:
        return []
    instance = {
        "id": instance_id,
        "name": "默认闲鱼店铺",
        "storeId": 0,
        "platformShopId": "",
        "storeName": "",
        "createdAt": "",
    }
    save_instances([instance])
    return [instance]


def save_instances(instances: list[dict]) -> None:
    path = instances_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"version": 1, "instances": instances}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def new_instance(store: dict) -> dict:
    store_id = int(store.get("id") or 0)
    platform_shop_id = str(store.get("platformShopId") or "").strip()
    return {
        "id": "store-" + uuid.uuid4().hex[:12],
        "name": str(store.get("storeName") or store.get("sellerNick") or f"闲鱼店铺 {store_id}").strip(),
        "storeId": store_id,
        "platformShopId": platform_shop_id,
        "storeName": str(store.get("storeName") or "").strip(),
        "createdAt": "",
    }


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
