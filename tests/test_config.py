"""配置解析与快捷键字符串转换的单元测试（标准库 unittest，无第三方依赖）。"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app.config.manager import Config, ConfigManager, config_dir
from app.clipboard.manager import looks_like_file_paths
from app.constants import DEFAULT_ENDPOINT, DEFAULT_SHORTCUT_ACCEL
from app.deepl.client import parse_error_body
from app.deepl.service import detect_direction
from app.shortcuts.accels import (
    gtk_accel_to_display,
    gtk_accel_to_xdg,
    is_valid_gtk_accel,
)


class ConfigDefaultsTest(unittest.TestCase):
    def test_defaults(self) -> None:
        config = Config()
        self.assertEqual(config.endpoint, DEFAULT_ENDPOINT)
        self.assertTrue(config.close_after_copy)
        self.assertFalse(config.start_on_login)
        self.assertTrue(config.hide_on_focus_loss)
        self.assertEqual(config.shortcut.preferred_trigger, DEFAULT_SHORTCUT_ACCEL)
        self.assertEqual(config.shortcut.backend, "portal")
        self.assertIsNone(config.shortcut.gsettings_path)

    def test_missing_file_gives_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            manager = ConfigManager(Path(tmp) / "config.json")
            self.assertEqual(manager.load().endpoint, DEFAULT_ENDPOINT)

    def test_broken_json_gives_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text("{ not json", encoding="utf-8")
            self.assertEqual(ConfigManager(path).load().endpoint, DEFAULT_ENDPOINT)

    def test_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            manager = ConfigManager(path)
            config = Config()
            config.shortcut.gsettings_path = (
                "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/custom3/"
            )
            config.close_after_copy = False
            config.log_level = "DEBUG"
            self.assertTrue(manager.save(config))

            loaded = manager.load()
            self.assertEqual(loaded.shortcut.gsettings_path, config.shortcut.gsettings_path)
            self.assertFalse(loaded.close_after_copy)
            self.assertEqual(loaded.log_level, "DEBUG")

            # 保存的内容里不应出现任何凭据字段
            raw = json.loads(path.read_text(encoding="utf-8"))
            self.assertNotIn("api_key", raw)
            self.assertNotIn("api_key", raw.get("shortcut", {}))

    def test_invalid_values_fall_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "close_after_copy": "yes",
                        "endpoint": 42,
                        "shortcut": {"backend": "unknown", "preferred_trigger": ""},
                    }
                ),
                encoding="utf-8",
            )
            loaded = ConfigManager(path).load()
            self.assertTrue(loaded.close_after_copy)
            self.assertEqual(loaded.endpoint, DEFAULT_ENDPOINT)
            self.assertEqual(loaded.shortcut.backend, "portal")
            self.assertEqual(loaded.shortcut.preferred_trigger, DEFAULT_SHORTCUT_ACCEL)

    def test_unknown_keys_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text(json.dumps({"future_option": True}), encoding="utf-8")
            self.assertTrue(ConfigManager(path).load().close_after_copy)

    def test_config_dir_follows_xdg(self) -> None:
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": "/tmp/xdg-test"}):
            self.assertTrue(str(config_dir()).startswith("/tmp/xdg-test"))


class AccelConversionTest(unittest.TestCase):
    def test_gtk_to_xdg(self) -> None:
        self.assertEqual(gtk_accel_to_xdg("<Control><Alt>space"), "CTRL+ALT+space")
        self.assertEqual(gtk_accel_to_xdg("<Super>t"), "LOGO+t")

    def test_gtk_to_display(self) -> None:
        self.assertEqual(gtk_accel_to_display("<Control><Alt>space"), "Ctrl+Alt+Space")

    def test_validity(self) -> None:
        self.assertTrue(is_valid_gtk_accel("<Control><Alt>space"))
        self.assertFalse(is_valid_gtk_accel("<Control><Alt>"))
        self.assertFalse(is_valid_gtk_accel(""))


class ClipboardHeuristicsTest(unittest.TestCase):
    def test_file_paths_detected(self) -> None:
        self.assertTrue(looks_like_file_paths("/home/user/a.png"))
        self.assertTrue(looks_like_file_paths("/home/user/a.png\n/home/user/b.png"))
        self.assertTrue(looks_like_file_paths("file:///home/user/a.png"))

    def test_normal_text_not_detected(self) -> None:
        self.assertFalse(looks_like_file_paths("How are you today?"))
        self.assertFalse(looks_like_file_paths("请把 /etc 下的配置发我"))
        self.assertFalse(looks_like_file_paths(""))
        self.assertFalse(looks_like_file_paths("https://example.com/a.png"))


class LanguageDirectionTest(unittest.TestCase):
    def test_english_goes_to_simplified_chinese(self) -> None:
        self.assertEqual(detect_direction("How are you today?"), ("EN", "ZH-HANS"))
        self.assertEqual(detect_direction("hello"), ("EN", "ZH-HANS"))

    def test_chinese_goes_to_american_english(self) -> None:
        self.assertEqual(detect_direction("你今天怎么样？"), ("ZH", "EN-US"))
        self.assertEqual(detect_direction("你好"), ("ZH", "EN-US"))

    def test_mixed_and_other_languages_fall_back_to_auto(self) -> None:
        self.assertEqual(detect_direction("你好 hello"), (None, "ZH-HANS"))
        # 注意：按规格 §15 的"拉丁字母 = 英文"规则，法语这类拉丁字母文本会被判成 EN。
        # 与 §17 的"Bonjour 走自动检测"存在矛盾，当前实现遵循 §15（见规格附录 A）。
        self.assertEqual(detect_direction("Bonjour tout le monde"), ("EN", "ZH-HANS"))
        self.assertEqual(detect_direction("12345 ！！！"), (None, "ZH-HANS"))


class DeepLErrorParsingTest(unittest.TestCase):
    class _Response:
        def __init__(self, payload):
            self._payload = payload

        def json(self):
            if isinstance(self._payload, Exception):
                raise self._payload
            return self._payload

    def test_flat_error_body(self) -> None:
        response = self._Response({"message": "Value for 'target_lang' not supported.", "code": "x"})
        self.assertEqual(parse_error_body(response), ("x", "Value for 'target_lang' not supported."))

    def test_nested_infrastructure_error_body(self) -> None:
        response = self._Response({"error": {"message": "Bad Gateway."}})
        self.assertEqual(parse_error_body(response), (None, "Bad Gateway."))

    def test_non_json_body(self) -> None:
        response = self._Response(ValueError("not json"))
        self.assertEqual(parse_error_body(response), (None, None))


if __name__ == "__main__":
    unittest.main()
