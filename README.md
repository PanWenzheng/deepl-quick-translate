# DeepL 快捷翻译（Ubuntu GNOME）

为 **Ubuntu 26.04 LTS + GNOME 50 + Wayland** 设计的极简桌面翻译工具：常驻后台，`Ctrl + Alt + Space` 唤起，自动带出剪贴板，`Enter` 翻译，`Ctrl + C` 复制译文并关闭。

> 定位：比打开浏览器更快的 DeepL 桌面入口。

## 状态

M1 已完成并在真机验证：单实例、配置管理、XDG 门户全局快捷键（`Ctrl+Alt+Space`）、
Spotlight 风格窗口骨架、热键按键守卫、激活去抖。M2 起接入剪贴板与输入细节。

- 完整规格说明：[docs/SPEC.md](docs/SPEC.md)
- DeepL API 核对结果：见规格说明书附录 B
- 门户实测结论：见规格说明书附录 C

## 开发运行

```sh
./tools/dev-install.sh     # 开发环境必需：装 .desktop（门户识别应用身份用）
./tools/dev-install.sh --remove   # 清理开发安装（装了 .deb 之后务必执行，否则会盖住系统级桌面文件）
./run.sh --background      # 后台常驻，等待 Ctrl+Alt+Space
./run.sh --toggle          # 唤起窗口
./run.sh --quit            # 退出并注销快捷键
./run.sh --set-api-key     # 把 DeepL API Key 写进系统密钥环（输入不回显）
python3 -m unittest discover -s tests
```

## 安装（.deb）

```sh
./packaging/build-deb.sh                            # 产物在 dist/
sudo apt install ./dist/deepl-quick-translate_*.deb # 用 apt 装可自动拉依赖
deepl-quick-translate --set-api-key                 # 设置 API Key
```

安装后应用菜单里会出现「DeepL 快捷翻译」，命令行入口 `deepl-quick-translate` 支持
`--toggle` / `--background` / `--settings` / `--quit` / `--set-api-key` /
`--api-key-status` / `--clear-api-key`。

> 这台开发机只有 `dpkg-deb`（没有 `dpkg-buildpackage` / `debhelper`），因此
> `build-deb.sh` 直接组装目录树再打包，不引入构建期依赖。

## 技术选型（已确认）

- 界面：GTK4 + libadwaita（PyGObject，随系统自带）
- 全局快捷键：XDG Desktop Portal `GlobalShortcuts`（备用 GSettings 绑定）
- 翻译：DeepL API `/v2/translate`，默认 endpoint `https://api-free.deepl.com`
- 语言方向：中文 `ZH → EN-US`，英文 `EN → ZH-HANS`，其他/混合 → `ZH-HANS`
- 应用 ID：`io.github.panwenzheng.DeepLQuickTranslate`，可执行文件 `deepl-quick-translate`
- 凭据：GNOME Secret Service（密钥环）
- 打包：`.deb`

## 目标平台

仅支持 Ubuntu 26.04 LTS / GNOME 50 / Wayland，不支持 X11、KDE、Windows、macOS。
