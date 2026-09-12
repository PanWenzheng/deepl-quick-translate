#!/usr/bin/env bash
# 开发环境安装：把 .desktop 与图标装到用户目录，让桌面环境与 XDG 门户认得这个应用。
#
# 为什么必需：GNOME 的 GlobalShortcuts 门户要求调用方有可识别的应用身份，
# 而 xdg-desktop-portal 是通过 app id 对应的 .desktop 文件（GAppInfo）来确认身份的。
# 缺了它，门户会以 "App info not found" / "An app id is required" 拒绝注册。
#
# 正式安装走 .deb（M5）；本脚本只用于开发联调。
set -euo pipefail

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
app_id="io.github.panwenzheng.DeepLQuickTranslate"
data_home="${XDG_DATA_HOME:-$HOME/.local/share}"
apps_dir="$data_home/applications"
icons_dir="$data_home/icons/hicolor/scalable/apps"

# 用户级 .desktop 优先级高于 /usr/share：装了 .deb 之后残留的开发副本会把系统级
# 那份整个盖住（症状：应用菜单里没有 Settings 动作、Exec 指向开发目录）。
pkg_installed=no
if dpkg -s deepl-quick-translate >/dev/null 2>&1; then
    pkg_installed=yes
fi

if [ "${1:-}" = "--remove" ]; then
    rm -f "$apps_dir/$app_id.desktop" "$icons_dir/$app_id.svg"
    command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$apps_dir" || true
    echo "已移除开发安装（用户级 .desktop 与图标）"
    exit 0
fi

if [ "$pkg_installed" = yes ] && [ "${1:-}" != "--force" ]; then
    echo "检测到已安装 .deb：用户级 .desktop 会覆盖系统级，导致菜单项与动作不一致。" >&2
    echo "如需清理开发安装：$0 --remove" >&2
    echo "确实要用开发版覆盖，请加 --force。" >&2
    exit 1
fi

mkdir -p "$apps_dir" "$icons_dir"

# Exec 指向仓库里的开发入口，这样未安装 .deb 也能真正被唤起
sed "s|^Exec=.*|Exec=$repo_dir/run.sh --toggle|" \
    "$repo_dir/data/$app_id.desktop" > "$apps_dir/$app_id.desktop"
cp "$repo_dir/data/icons/$app_id.svg" "$icons_dir/$app_id.svg"

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$apps_dir" || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t -f "$data_home/icons/hicolor" 2>/dev/null || true

echo "installed:"
echo "  $apps_dir/$app_id.desktop"
echo "  $icons_dir/$app_id.svg"
