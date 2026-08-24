from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

try:
    import oss2
except ImportError:  # pragma: no cover - only used in GitHub Actions
    oss2 = None  # type: ignore[assignment]


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload gf-auto Windows release and latest manifest to OSS.")
    parser.add_argument("--zip", required=True, type=Path)
    parser.add_argument("--installer", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--access-key-id", required=True)
    parser.add_argument("--access-key-secret", required=True)
    parser.add_argument("--prefix", default="assets/releases/gf-auto")
    parser.add_argument("--public-base-url", default="https://img.yinghuasuan.com/assets/releases/gf-auto")
    args = parser.parse_args()

    if oss2 is None:
        raise RuntimeError("oss2 is not installed")
    if not args.zip.is_file() or not args.installer.is_file():
        raise FileNotFoundError("release zip or installer not found")

    version = args.version.strip().lstrip("v")
    tag = f"v{version}"
    prefix = args.prefix.strip("/")
    public_base_url = args.public_base_url.rstrip("/")
    zip_key = f"{prefix}/{tag}/{args.zip.name}"
    installer_key = f"{prefix}/{tag}/{args.installer.name}"
    manifest_key = f"{prefix}/latest.json"
    manifest = {
        "version": version,
        "tag": tag,
        "platform": "win64",
        "filename": args.zip.name,
        "url": f"{public_base_url}/{tag}/{args.zip.name}",
        "sha256": _sha256_file(args.zip),
        "size": args.zip.stat().st_size,
        "installerFilename": args.installer.name,
        "installerUrl": f"{public_base_url}/{tag}/{args.installer.name}",
        "installerSha256": _sha256_file(args.installer),
        "installerSize": args.installer.stat().st_size,
        "publishedAt": datetime.now(timezone.utc).isoformat(),
    }
    bucket = oss2.Bucket(
        oss2.Auth(args.access_key_id, args.access_key_secret),
        args.endpoint,
        args.bucket,
    )
    headers = {"Cache-Control": "public, max-age=31536000"}
    bucket.put_object_from_file(zip_key, str(args.zip), headers=headers)
    bucket.put_object_from_file(installer_key, str(args.installer), headers=headers)
    bucket.put_object(
        manifest_key,
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-cache"},
    )
    print(json.dumps(manifest, ensure_ascii=False))
    return 0


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    sys.exit(main())
