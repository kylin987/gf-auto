#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

python3 -m pip install -r requirements.txt pyinstaller

if [ ! -f node_bin/node ]; then
    echo "Downloading Node.js for macOS..."
    VERSION=v20.18.0
    ARCH=$(uname -m)
    case "$ARCH" in
        arm64) NODE_ARCH=arm64 ;;
        *) NODE_ARCH=x64 ;;
    esac
    mkdir -p node_bin
    TARBALL="node-${VERSION}-darwin-${NODE_ARCH}.tar.gz"
    curl -fsSL "https://nodejs.org/dist/${VERSION}/${TARBALL}" -o "/tmp/${TARBALL}"
    tar -xzf "/tmp/${TARBALL}" -C /tmp
    cp "/tmp/node-${VERSION}-darwin-${NODE_ARCH}/bin/node" node_bin/node
    rm -rf "/tmp/node-${VERSION}-darwin-${NODE_ARCH}" "/tmp/${TARBALL}"
fi

if ! grep -q "def check_chrome_installed" cookie_auth.py; then
    echo "ERROR: cookie_auth.py is outdated, missing check_chrome_installed"
    exit 1
fi

rm -rf build dist
python3 -m PyInstaller --clean --noconfirm xianyu.spec

echo ""
echo "Build done: dist/XianYuApis"
