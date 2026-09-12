#!/usr/bin/env bash
# 构建 .deb：直接组装目录树后交给 dpkg-deb，不依赖 dpkg-buildpackage / debhelper，
# 因此在只装了基础工具的环境里也能打包。
#
# 用法：packaging/build-deb.sh [输出目录]
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
out_dir="${1:-$repo_dir/dist}"
app_id="io.github.panwenzheng.DeepLQuickTranslate"
pkg_name="deepl-quick-translate"
version="$(python3 -c "import sys; sys.path.insert(0, '$repo_dir'); from app.constants import VERSION; print(VERSION)")"
arch="all"

staging="$(mktemp -d)"
trap 'rm -rf "$staging"' EXIT

install -d "$staging/DEBIAN"
install -d "$staging/usr/bin"
install -d "$staging/usr/share/deepl-quick-translate"
install -d "$staging/usr/share/applications"
install -d "$staging/usr/share/icons/hicolor/scalable/apps"
install -d "$staging/usr/share/doc/$pkg_name"

# 应用本体：装到 /usr/share 下，由启动脚本设置 PYTHONPATH
cp -r "$repo_dir/app" "$staging/usr/share/deepl-quick-translate/app"
find "$staging/usr/share/deepl-quick-translate" -name '__pycache__' -type d -prune -exec rm -rf {} +

cat > "$staging/usr/bin/$pkg_name" <<'LAUNCHER'
#!/bin/sh
# DeepL 快捷翻译启动器
#
# --toggle（或不带参数）时优先走 D-Bus 快路径：主实例已在运行时，一次调用就能
# 把窗口唤起来，省掉启动整个 Python 解释器的 ~250ms 开销；主实例没在跑时
# 调用会失败，自然落到下面的正常启动流程。
if [ "$#" -eq 0 ] || { [ "$#" -eq 1 ] && [ "$1" = "--toggle" ]; }; then
    if command -v gdbus >/dev/null 2>&1 && gdbus call --session \
        --dest io.github.panwenzheng.DeepLQuickTranslate \
        --object-path /io/github/panwenzheng/DeepLQuickTranslate \
        --method org.freedesktop.Application.Activate "{}" >/dev/null 2>&1; then
        exit 0
    fi
fi

PYTHONPATH="/usr/share/deepl-quick-translate${PYTHONPATH:+:$PYTHONPATH}"
export PYTHONPATH
exec python3 -m app "$@"
LAUNCHER
chmod 0755 "$staging/usr/bin/$pkg_name"

install -m 0644 "$repo_dir/data/$app_id.desktop" "$staging/usr/share/applications/$app_id.desktop"
install -m 0644 "$repo_dir/data/icons/$app_id.svg" \
    "$staging/usr/share/icons/hicolor/scalable/apps/$app_id.svg"
# 同时提供标准尺寸 PNG：避免桌面环境对 SVG 的额外解释（例如按符号化图标处理时
# 把所有填充色替换成单色，图形就变成一个纯色方块）
python3 "$repo_dir/packaging/make-icons.py" \
    "$repo_dir/data/icons/$app_id.svg" "$staging/usr/share/icons/hicolor" >/dev/null
install -m 0644 "$repo_dir/docs/SPEC.md" "$staging/usr/share/doc/$pkg_name/SPEC.md"
install -m 0644 "$repo_dir/README.md" "$staging/usr/share/doc/$pkg_name/README.md"

# Debian 策略要求包内带 copyright（DEP-5）。许可证正文从仓库根的 LICENSE 生成，
# 避免包里的副本和仓库里的正文各写一份、日后改一端忘另一端。
{
    cat <<EOF
Format: https://www.debian.org/doc/packaging-manuals/copyright-format/1.0/
Upstream-Name: $pkg_name
Source: https://github.com/PanWenzheng/$pkg_name

Files: *
Copyright: 2026 Pan Wenzheng and DeepSeek
License: MIT
$(sed 's/^/ /' "$repo_dir/LICENSE")
EOF
} > "$staging/usr/share/doc/$pkg_name/copyright"

# 规范化权限：Debian 包内不应出现组可写，根目录也不能是 mktemp 的 0700
chmod 0755 "$staging"
find "$staging" -type d -exec chmod 0755 {} +
find "$staging" -type f -exec chmod 0644 {} +
chmod 0755 "$staging/usr/bin/$pkg_name"

cat > "$staging/DEBIAN/control" <<CONTROL
Package: $pkg_name
Version: $version
Section: utils
Priority: optional
Architecture: $arch
Maintainer: Pan Wenzheng <237622581+PanWenzheng@users.noreply.github.com>
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1, python3-httpx, gir1.2-secret-1, libglib2.0-bin
Description: 极简的 DeepL 桌面快捷翻译工具
 常驻后台，按 Ctrl+Alt+Space 唤起窗口，自动带入剪贴板内容，
 Enter 翻译、Ctrl+C 复制译文并关闭。
 .
 面向 Ubuntu 26.04 + GNOME 50 + Wayland 设计。
CONTROL

mkdir -p "$out_dir"
dpkg-deb --build --root-owner-group "$staging" "$out_dir/${pkg_name}_${version}_${arch}.deb"
echo "built: $out_dir/${pkg_name}_${version}_${arch}.deb"
