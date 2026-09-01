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


def instances_file(pub_id: int | str | None = None) -> Path:
    try:
        normalized_pub_id = int(pub_id or 0)
    except (TypeError, ValueError):
        normalized_pub_id = 0
    if normalized_pub_id > 0:
        return app_data_dir() / "pubs" / str(normalized_pub_id) / INSTANCES_FILE_NAME
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


def _read_instances(path: Path) -> list[dict]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        instances = payload.get("instances", []) if isinstance(payload, dict) else []
        if isinstance(instances, list):
            return [item for item in instances if isinstance(item, dict) and item.get("id")]
    except (OSError, ValueError, TypeError):
        pass
    return []


def load_instances(pub_id: int | str | None = None, authorized_stores: list[dict] | None = None) -> list[dict]:
    """按商户和当前授权店铺读取实例，首次升级时迁移旧全局配置。"""
    path = instances_file(pub_id)
    if path.is_file():
        instances = _read_instances(path)
        if authorized_stores is None:
            return instances
        authorized_ids = {
            int(store.get("id") or 0)
            for store in authorized_stores
            if isinstance(store, dict) and int(store.get("id") or 0) > 0
        }
        instances = [
            instance
            for instance in instances
            if int(instance.get("storeId") or 0) in authorized_ids
        ]
        save_instances(instances, pub_id)
        return instances

    try:
        normalized_pub_id = int(pub_id or 0)
    except (TypeError, ValueError):
        normalized_pub_id = 0
    if normalized_pub_id > 0:
        stores = authorized_stores or []
        authorized_ids = {int(store.get("id") or 0) for store in stores if isinstance(store, dict)}
        authorized_platform_ids = {
            str(store.get("platformShopId") or "").strip()
            for store in stores if isinstance(store, dict) and str(store.get("platformShopId") or "").strip()
        }
        migrated = []
        for instance in _read_instances(instances_file()):
            store_id = int(instance.get("storeId") or 0)
            if store_id in authorized_ids:
                migrated.append(instance)
                continue
            if store_id <= 0 and authorized_platform_ids:
                actual_shop_id = chrome_account_from_cookie_file(
                    instance_cookie_file(instance["id"])
                ).get("userId")
                if str(actual_shop_id or "") in authorized_platform_ids:
                    migrated.append(instance)
        if migrated:
            save_instances(migrated, normalized_pub_id)
        return migrated

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


def save_instances(instances: list[dict], pub_id: int | str | None = None) -> None:
    path = instances_file(pub_id)
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
    return chrome_account_from_cookie_file(default_cookie_file())


def chrome_account_from_cookie_file(cookie_file: str) -> dict[str, str]:
    try:
        payload = json.loads(Path(cookie_file).read_text(encoding="utf-8"))
        cookies = payload.get("cookies", payload) if isinstance(payload, dict) else {}
        if not isinstance(cookies, dict):
            return {}
        return {
            "nick": unquote(str(cookies.get("tracknick") or "")).strip(),
            "userId": str(cookies.get("unb") or "").strip(),
        }
    except (OSError, ValueError, TypeError):
        return {}
