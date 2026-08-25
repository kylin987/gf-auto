"""Signed-manifest update support for the Windows desktop client."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlparse

import requests


from typing import Callable


DEFAULT_UPDATE_MANIFEST_URL = (
    "https://img.yinghuasuan.com/assets/releases/gf-auto/latest.json"
)
_TIMEOUT_SECONDS = 20
_CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    installer_url: str
    installer_sha256: str
    installer_filename: str
    installer_size: int
    published_at: str = ""


def check_for_update(current_version: str) -> UpdateInfo | None:
    response = requests.get(DEFAULT_UPDATE_MANIFEST_URL, timeout=10)
    response.raise_for_status()
    manifest = response.json()
    if not isinstance(manifest, dict):
        raise RuntimeError("更新清单格式不正确")

    info = _parse_manifest(manifest)
    if _compare_versions(info.version, current_version) <= 0:
        return None
    return info


def download_installer(
    info: UpdateInfo,
    progress_callback: Callable[[int, int], None] | None = None,
) -> Path:
    update_dir = _update_dir()
    update_dir.mkdir(parents=True, exist_ok=True)
    target = update_dir / info.installer_filename
    if target.is_file() and _sha256_file(target).lower() == info.installer_sha256:
        if progress_callback:
            progress_callback(info.installer_size, info.installer_size)
        return target

    partial = target.with_suffix(target.suffix + ".download")
    with requests.get(info.installer_url, stream=True, timeout=_TIMEOUT_SECONDS) as response:
        response.raise_for_status()
        total_size = int(response.headers.get("content-length", 0) or 0) or info.installer_size
        downloaded = 0
        if progress_callback:
            progress_callback(0, total_size)
        with partial.open("wb") as handle:
            for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                if chunk:
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total_size)

    if _sha256_file(partial).lower() != info.installer_sha256:
        partial.unlink(missing_ok=True)
        raise RuntimeError("更新包校验失败，请稍后重试")
    partial.replace(target)
    return target


def launch_installer(installer_path: Path) -> None:
    if not installer_path.is_file():
        raise FileNotFoundError(f"找不到更新安装包：{installer_path}")
    if os.name != "nt":
        raise RuntimeError("在线安装仅支持 Windows 客户端")
    subprocess.Popen([str(installer_path)], cwd=str(installer_path.parent))


def _parse_manifest(manifest: dict) -> UpdateInfo:
    version = str(manifest.get("version", "")).strip().lstrip("v")
    installer_url = str(manifest.get("installerUrl", "")).strip()
    installer_sha256 = str(manifest.get("installerSha256", "")).strip().lower()
    installer_filename = str(manifest.get("installerFilename", "")).strip()
    if not installer_filename and installer_url:
        installer_filename = Path(urlparse(installer_url).path).name
    try:
        installer_size = int(manifest.get("installerSize", 0) or 0)
    except (TypeError, ValueError):
        installer_size = 0

    if not version or not installer_url or not installer_sha256 or not installer_filename:
        raise RuntimeError("更新清单缺少安装包字段")
    if urlparse(installer_url).scheme != "https":
        raise RuntimeError("更新地址必须使用 HTTPS")
    if not re.fullmatch(r"[0-9a-f]{64}", installer_sha256):
        raise RuntimeError("更新清单中的安装包校验值不正确")
    return UpdateInfo(
        version=version,
        installer_url=installer_url,
        installer_sha256=installer_sha256,
        installer_filename=installer_filename,
        installer_size=installer_size,
        published_at=str(manifest.get("publishedAt", "")).strip(),
    )


def _update_dir() -> Path:
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    return Path(root) / "yhs-fish-plugin" / "updates"


def _compare_versions(left: str, right: str) -> int:
    left_parts = _version_parts(left)
    right_parts = _version_parts(right)
    width = max(len(left_parts), len(right_parts))
    left_parts.extend([0] * (width - len(left_parts)))
    right_parts.extend([0] * (width - len(right_parts)))
    if left_parts == right_parts:
        return 0
    return 1 if left_parts > right_parts else -1


def _version_parts(version: str) -> list[int]:
    return [int(item) for item in re.findall(r"\d+", version)] or [0]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()
