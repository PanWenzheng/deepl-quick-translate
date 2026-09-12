"""不需要图形界面的本地命令。

这些命令必须在 GApplication 之前处理：它们读写密钥环或打印状态，不应该被转发给
已经在运行的主实例（那样提示会出现在后台进程的 stdin/stdout 上）。
"""

from __future__ import annotations

import getpass
import sys

from .config.secret import ENV_VAR, SecretManager

LOCAL_COMMANDS = ("--set-api-key", "--clear-api-key", "--api-key-status")


def is_local_command(args: list[str]) -> bool:
    return any(arg in LOCAL_COMMANDS for arg in args)


def handle_local_command(args: list[str]) -> int:
    """返回进程退出码。"""
    secrets = SecretManager()

    if "--set-api-key" in args:
        return _set_api_key(secrets)
    if "--clear-api-key" in args:
        return _clear_api_key(secrets)
    if "--api-key-status" in args:
        return _api_key_status(secrets)
    return 0


def _set_api_key(secrets: SecretManager) -> int:
    try:
        api_key = getpass.getpass("请输入 DeepL API Key（输入不回显）：").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n已取消。", file=sys.stderr)
        return 1
    if not api_key:
        print("没有输入内容，已取消。", file=sys.stderr)
        return 1
    if not secrets.store(api_key):
        print(
            "保存失败：系统密钥环不可用。\n"
            f"可改用环境变量 {ENV_VAR} 提供 Key（程序不会把它写入任何文件）。",
            file=sys.stderr,
        )
        return 1
    print(f"已保存到系统密钥环：{secrets.status()[1]}")
    return 0


def _clear_api_key(secrets: SecretManager) -> int:
    if not secrets.clear():
        print("删除失败：密钥环里没有找到 Key，或系统密钥环不可用。", file=sys.stderr)
        return 1
    print("已从系统密钥环删除 API Key。")
    return 0


def _api_key_status(secrets: SecretManager) -> int:
    configured, masked, source = secrets.status()
    if not configured:
        print(f"未配置 API Key。可用 `--set-api-key` 写入密钥环，或设置 {ENV_VAR}。")
        return 1
    origin = "系统密钥环" if source == "keyring" else f"环境变量 {ENV_VAR}"
    print(f"已配置 API Key：{masked}（来源：{origin}）")
    return 0
