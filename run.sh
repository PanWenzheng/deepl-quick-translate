#!/usr/bin/env bash
# 开发直跑：./run.sh [--toggle|--background|--settings|--quit|--verbose]
set -euo pipefail
cd "$(dirname "$0")"

# 快捷键/菜单唤起时的快路径：若主实例已在运行，一次 D-Bus 调用即可把它唤起来。
# 实测直接启动 Python 解释器要 ~250ms（解释器 + GI 导入），而这条路径 <10ms。
# 只在参数恰好是"空"或"--toggle"时启用，避免吞掉 --verbose 之类的调试参数。
if [ "$#" -eq 0 ] || { [ "$#" -eq 1 ] && [ "$1" = "--toggle" ]; }; then
    if command -v gdbus >/dev/null 2>&1 && gdbus call --session \
        --dest io.github.panwenzheng.DeepLQuickTranslate \
        --object-path /io/github/panwenzheng/DeepLQuickTranslate \
        --method org.freedesktop.Application.Activate "{}" >/dev/null 2>&1; then
        exit 0
    fi
fi

exec python3 -m app "$@"
