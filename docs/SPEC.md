# DeepL 快捷翻译 · V1 规格说明书

| 项 | 值 |
| --- | --- |
| 文档版本 | v1.1（待评审） |
| 日期 | 2026-09-12 |
| 状态 | Draft · §12 的 Q1–Q8 全部已确认，等待开工 |
| 上游需求 | 用户提供的《Ubuntu GNOME 极简 DeepL 快捷翻译工具》50 节需求文档 |
| 目标平台 | Ubuntu 26.04 LTS · GNOME 50 · Wayland |

---

## 0. 一句话定义

> 比打开浏览器更快的 DeepL 桌面入口。

常驻后台，一个全局快捷键 `Ctrl + Alt + Space` 唤起，自动带出剪贴板内容，`Enter` 翻译，`Ctrl + C` 复制译文并关闭。

---

## 1. 目标平台（已在本机实测确认）

本节所有版本号均为在目标机器上实际执行命令得到的输出，非推测。

| 项目 | 实测结果 | 命令来源 |
| --- | --- | --- |
| 发行版 | Ubuntu 26.04.1 LTS (resolute) | `lsb_release -a` |
| 桌面会话 | `ubuntu:GNOME`，`XDG_SESSION_TYPE=wayland` | 环境变量 |
| GNOME Shell | 50.1（`gnome-shell 50.1-0ubuntu1.2`） | `gnome-shell --version` / `dpkg` |
| Python | 3.14.4（`/usr/bin/python3`） | `python3 -V` |
| GTK | 4.22.4，`gir1.2-gtk-4.0` 已安装且可 `import` | `gi.require_version('Gtk','4.0')` |
| libadwaita | 1.9.1（`gir1.2-adw-1`） | 同上 |
| 桌面门户 | `xdg-desktop-portal` 1.21.1 + `xdg-desktop-portal-gnome` 50.0 | `dpkg` |
| 全局快捷键门户 | `org.freedesktop.portal.GlobalShortcuts` **存在**，`version = 1`，方法：`CreateSession` / `BindShortcuts` / `ListShortcuts` / `ConfigureShortcuts` | 会话 D-Bus introspection |
| 密钥环 | `gnome-keyring 50.0`，`libsecret 0.21.7` | `dpkg` |
| 运行环境 | 具备活动会话总线 `/run/user/1000/bus`，`WAYLAND_DISPLAY=wayland-0` | 环境变量 |

**结论：目标平台能力齐备，无需引入任何重量级运行时依赖即可开工。**

明确不支持：X11、KDE、Windows、macOS。

---

## 2. 技术选型与决策记录

| ID | 决策 | 理由 | 被否方案 |
| --- | --- | --- | --- |
| **D1** | 工具包：**GTK4 + libadwaita（PyGObject）** | 已随系统安装（零额外依赖）；Wayland 原生；中文输入法走 GTK 原生 IM context，preedit 处理最可靠；外观与 GNOME 50 一致；打包体积小 | PySide6：apt 有 6.10.2 但需额外安装数百 MB，且 Python 3.14 + Qt Wayland IME 需要额外验证 |
| **D2** | 全局快捷键：**门户为主 + GSettings 兜底** | 门户是文档指定的标准路径且确认可用；GSettings `media-keys` 自定义快捷键可在门户不可用时保证功能 | 仅门户（首次必须用户手动确认，单点失败）；仅 GSettings（不是标准路径） |
| **D3** | 单实例：**`Gtk.Application` 的 D-Bus 单实例机制** | GApplication 自带"第二个实例把参数转交主实例后退出"，天然满足需求；免去手写锁文件/D-Bus 名 | 手写 D-Bus 名抢占、锁文件 |
| **D4** | HTTP 客户端：**httpx**（`python3-httpx` 0.28.1） | 原生 async、超时/取消语义清晰、依赖小 | `requests`（同步，需线程池）；DeepL 官方 SDK（功能超出 V1 需要，且多一层封装） |
| **D5** | 凭据：**Secret Service（`python3-secretstorage`）** | 文档明确要求，系统已有 gnome-keyring | 明文 JSON |
| **D6** | 配置：`~/.config/<app_id>/config.json`，仅存非敏感项 | 简单、可读、便于用户排查 | 数据库（V1 明确不做） |
| **D7** | 打包：`.deb`（`dpkg-buildpackage` + `dh-python`） | 文档优先要求，Ubuntu 原生 | Flatpak / Snap / AppImage |
| **D8** | 窗口定位：**不做定位算法** | Wayland 下客户端无法自行定位窗口，统一由合成器居中；符合原文档 §7 | 手动计算屏幕坐标（Wayland 下不可行） |
| **D9** | Endpoint 默认：`https://api-free.deepl.com` | 用户确认为 DeepL **Free** 账户；Pro 端点保留在设置中 | — |
| **D10** | 语言代码：源语言 `ZH` / `EN`，目标语言显式用 `ZH-HANS` / `EN-US` | 官方语言表中 `EN`（all variants）与 `ZH`（unspecified variant）均为合法且未弃用；目标语言显式指定变体可让输出可预期 | 源与目标统一用 `ZH`/`EN`（输出变体由 DeepL 决定） |
| **D11** | 错误解析：兼容 `{message, code}` 与 `{error: {message}}` 两种形态，按状态码 + `code` 分支 | 官方明确网关/基础设施错误使用嵌套形态，且要求不要匹配 `message` 文本 | 只读顶层 `message` |
| **D12** | 记录响应头 `X-Trace-ID` | 官方要求默认记录，用于排障与提工单；该值不含用户内容，符合隐私要求 | 不记录 |

### 2.1 命名与标识符（已定）

| 用途 | 取值 |
| --- | --- |
| GApplication ID / 会话总线名 | `io.github.panwenzheng.DeepLQuickTranslate` |
| `.desktop` 文件名 | `io.github.panwenzheng.DeepLQuickTranslate.desktop` |
| 图标名 | `io.github.panwenzheng.DeepLQuickTranslate.svg` |
| 可执行文件 | `deepl-quick-translate` |
| 配置目录 | `~/.config/io.github.panwenzheng.DeepLQuickTranslate/` |
| Secret Service service 名 | `io.github.panwenzheng.DeepLQuickTranslate` |
| 界面显示名 | DeepL 快捷翻译 |
| 项目仓库 | `github.com/PanWenzheng`（GitHub 用户名为 `PanWenzheng`；反向域名部分按 GNOME/Flatpak 约定写小写） |

下文用 `<app_id>` 代指上表第一行的值。

---

## 3. 核心交互流程

```
Ctrl + Alt + Space
        │
        ▼
（单实例守护进程收到激活）
        │
        ├─ 显示窗口 ── 置前 ── 取得键盘焦点
        │                              │
        │                              ▼
        │                    读取剪贴板 text/plain（仅此一次）
        │                              │
        │                    ┌─────────┴──────────┐
        │                    ▼                    ▼
        │              有纯文本              无纯文本
        │                    │                    │
        │                    ▼                    ▼
        │         与上次内容不同？覆盖      空输入框
        │         与上次内容相同？保留当前编辑
        │                    └─────────┬──────────┘
        │                              ▼
        │                    用户输入 / 修改（支持中文输入法）
        │                              ▼
        │                          Enter（提交）
        │                              ▼
        │                     语言判定 → DeepL /v2/translate
        │                              ▼
        │                        显示译文（Loading 期间可 Esc 取消）
        │                              ▼
        │                          Ctrl + C
        │                              ▼
        │                    复制译文 → 隐藏窗口（进程保活）
        ▼
   （等待下一次唤起）
```

**关键顺序约束**：在 Wayland 下，Mutter 只允许持有键盘焦点的客户端读取剪贴板。因此实现必须严格遵循 **先显示窗口、取得焦点，再读剪贴板**，不得提前读取。

---

## 4. 功能规格

需求编号规则：`FR-<模块>-<序号>`，供后续测试用例逐条对应。

### 4.1 全局快捷键

| 编号 | 规格 |
| --- | --- |
| FR-SHORTCUT-1 | 默认快捷键为 `Ctrl + Alt + Space`，且全局有效（窗口未聚焦时同样生效）。 |
| FR-SHORTCUT-2 | 通过 XDG Desktop Portal `org.freedesktop.portal.GlobalShortcuts` 注册：`CreateSession` → `BindShortcuts`（`preferred_trigger = <Control><Alt>space`）→ 监听 `Activated` 信号。 |
| FR-SHORTCUT-3 | **首次注册必有一次用户确认**：GNOME 的门户实现会忽略 `preferred_trigger`，弹出系统窗口要求用户按下目标组合键。这是平台行为，不是缺陷，需要在首次启动引导与设置页面中明确告知用户。 |
| FR-SHORTCUT-4 | 该确认**每个应用只出现一次**：GNOME 按 app id 记住绑定，之后每次启动静默恢复（实测第二次注册 0.3s 内完成、无对话框）。GlobalShortcuts 协议里**没有** `restore_token` 之类的持久化字段，无需自行保存令牌；只有用户清除系统设置或改绑才会再次提示。 |
| FR-SHORTCUT-5 | 门户会话（session）与进程同生命周期；进程退出即注销绑定。 |
| FR-SHORTCUT-6 | 注册失败必须在 UI 明确提示 `Shortcut unavailable`，并引导用户进入设置更换快捷键；**不得静默失败**。 |
| FR-SHORTCUT-7 | 兜底路径：当门户不可用或用户拒绝授权时，支持通过 GSettings `org.gnome.settings-daemon.plugins.media-keys` 的自定义快捷键绑定，触发命令 `deepl-quick-translate --toggle`（由已在运行的主实例接管）。此路径可精确静默绑定 `Ctrl+Alt+Space`，代价是写入用户 dconf，需在设置页明示。 |
| FR-SHORTCUT-8 | 用户可在设置中更换快捷键（门户路径用 `ConfigureShortcuts`，GSettings 路径直接改写条目）。 |
| FR-SHORTCUT-9 | 全局快捷键只此一个，不设计第二个。 |
| FR-SHORTCUT-10 | 门户要求调用方具备可识别的应用身份：系统数据目录中必须存在 `<app_id>.desktop`，否则 xdg-desktop-portal 先以 `App info not found for '<app_id>'` 拒绝宿主注册，再以 `An app id is required` 拒绝 `CreateSession`。因此 `.desktop` 是门户路径的**硬依赖**（.deb 安装时提供；开发环境用 `tools/dev-install.sh` 装到 `~/.local/share`）。 |

### 4.2 单实例

| 编号 | 规格 |
| --- | --- |
| FR-INSTANCE-1 | 同一时刻只允许一个实例。 |
| FR-INSTANCE-2 | 由 `Gtk.Application` 的 `application_id` 抢占会话总线名实现；第二个进程的启动参数通过 D-Bus 转交主实例处理，随后自身退出，不驻留。 |
| FR-INSTANCE-3 | 参数语义：`--toggle`（唤起翻译窗口，默认行为）、`--settings`（打开设置窗口）、`--quit`（退出主实例）。 |
| FR-INSTANCE-4 | 重复执行 `./deepl-quick-translate` 等价于 `--toggle`。 |

### 4.3 窗口与唤起

| 编号 | 规格 |
| --- | --- |
| FR-WINDOW-1 | 无论来自快捷键还是命令行，最终都进入**同一个**翻译窗口。 |
| FR-WINDOW-2 | 唤起顺序固定为：显示 → 置前 → 取得焦点 → 读取剪贴板。 |
| FR-WINDOW-3 | Spotlight / Raycast 风格：无标题栏、圆角、宽度固定（约 640–720 px）、高度随内容自适应、尽量小。 |
| FR-WINDOW-4 | 由合成器负责居中与多显示器定位，程序不实现定位算法。 |
| FR-WINDOW-5 | 失去焦点时是否自动隐藏：默认**隐藏**（可在设置中关闭）。此项为原文档未覆盖的行为，见 §12 开放问题。 |
| FR-WINDOW-6 | 首帧渲染不得等待剪贴板读取、配置读取或网络初始化（见 §8 性能）。 |
| FR-WINDOW-7 | **热键按键守卫**：触发快捷键中的那个按键（默认 `Space`）会在窗口拿到焦点后被合成器补投递过来（实测比 `present()` 晚约 570 ms）。更麻烦的是该键的**松开事件往往根本送不到窗口**，GTK 因此认为它一直被按着，按系统重复率**无限重复**（实测 3 秒内 80+ 次、5 秒内 160+ 次，间隔约 27 ms），表现为"窗口开着就自己冒空格"。对策有两条：①把门户给的 `activation_token` 通过 `Gdk.Toplevel.set_startup_id()` 交给合成器，走正规激活路径；②在捕获阶段吞掉该键的按下事件，结束条件为**用户按下了别的键**、该键松开、或 30 s 兜底上限——**绝不能按短时间到期**，否则按键还在重复时守卫就失效了。代价：窗口刚出现时若用户想以该触发键本身作为第一个字符，会被吞掉一次。 |
| FR-WINDOW-8 | **激活去抖**：按住快捷键时 GNOME 会连续触发 `Activated`，两次激活间隔小于 300 ms 时忽略后一次，避免窗口被反复呈现。 |

### 4.4 剪贴板

| 编号 | 规格 |
| --- | --- |
| FR-CLIP-1 | 只读取 `text/plain`。若剪贴板仅含图片、HTML、文件、富文本等，则按"无文本"处理，显示空输入框。特别地：**文件管理器复制文件时也会提供一份 `text/plain`，内容是文件路径**，此时剪贴板还会带 `x-special/gnome-copied-files` / `text/uri-list` 等标记；当这些标记存在且文本本身就是路径列表（每行都是绝对路径或 `file://` URI）时，一律按"无文本"处理。 |
| FR-CLIP-2 | 每次唤起只读取一次，绝不监听剪贴板的持续变化。 |
| FR-CLIP-3 | 程序自身写入（复制译文）不会触发任何再翻译逻辑——因为不存在剪贴板监听，天然满足。 |
| FR-CLIP-4 | 读取到的文本填入输入框，用户可自由编辑。 |
| FR-CLIP-5 | 写入译文时以 `text/plain` 提供选择区数据；**窗口只隐藏、进程不退出**，由常驻进程继续持有 selection。否则在 Wayland 下用户切到其他应用粘贴会得到空内容。 |
| FR-CLIP-6 | 超长剪贴板文本（超过 §4.8 限制）仍填入输入框，但提交时本地提示，不发请求。 |
| FR-CLIP-7 | **仅当剪贴板内容与上次填入的不同时才覆盖输入框**。内容相同则完全不触碰输入框（不覆盖、不改选区、不动光标），因此用户编辑到一半切去复制别的东西再回来，自己的修改不会被冲掉。判定依据是"上次实际填入的内容"，其中"没有文本"（含剪贴板为空、被识别为文件、非纯文本）也作为一个取值参与比较。 |

### 4.5 输入框与中文输入法

| 编号 | 规格 |
| --- | --- |
| FR-INPUT-1 | 唤起后自动获得输入焦点。若输入框已有内容（上一次的输入，或刚填入的剪贴板文本），则**整体选中**，用户直接键入即可替换；无内容时显示占位文案。 |
| FR-INPUT-2 | 支持中文输入法、英文输入、粘贴、全选、编辑、光标移动。 |
| FR-INPUT-3 | 支持多行文本；输入框高度随内容自动增长，上限约 40% 屏高后内部滚动。 |
| FR-INPUT-4 | **输入法组合（preedit）期间，`Enter` 用于确认候选词或上屏原始字母，绝不能提交翻译**；只有输入法提交文字后用户再次按 `Enter` 才触发翻译。实现方式：提交挂在输入控件的 `activate` 信号上，组合期间 GTK 的 IM context 会先消费 `Enter`，该信号不会被触发；`Esc` 则放在冒泡阶段的按键控制器里处理，组合中会被输入法优先用于取消候选，窗口不会关闭。 |
| FR-INPUT-5 | 多行与"Enter 提交"的冲突按此约定解决：`Enter` 提交翻译，`Shift + Enter` 插入换行。 |
| FR-INPUT-6 | 输入框左侧显示放大镜图标，占位文案 `输入要翻译的内容`。 |

### 4.6 提交与取消

| 编号 | 规格 |
| --- | --- |
| FR-SUBMIT-1 | `Enter` 提交翻译。 |
| FR-SUBMIT-2 | 请求进行中再次按 `Enter` 一律忽略，防止重复请求。 |
| FR-SUBMIT-3 | 输入为空（或仅空白）时 `Enter` 不发送请求，窗口保持打开。 |
| FR-SUBMIT-4 | `Esc` 在任何状态下关闭（隐藏）窗口。 |
| FR-SUBMIT-5 | 请求进行中按 `Esc`：先取消本地请求、立即隐藏窗口，随后安全结束底层 HTTP 请求。 |
| FR-SUBMIT-6 | 请求被取消或窗口已关闭后，迟到的响应必须被丢弃，绝不能更新 UI。 |
| FR-SUBMIT-7 | 结果显示后修改原文再按 `Enter` 可重新翻译，不提供额外 Retry 按钮。 |

### 4.7 翻译方向判定

输入文本在本地做简单判定，不使用网络请求做语言检测。

| 输入特征 | `source_lang` | `target_lang` |
| --- | --- | --- |
| 主要为中文（Han 字符） | `ZH` | `EN-US` |
| 主要为英文（拉丁字母） | `EN` | `ZH-HANS` |
| 明显混合（同时含 Han 与拉丁字母，例如 `你好 hello`） | 不传（自动检测） | `ZH-HANS` |
| 其他语言（例如 `Bonjour`） | 不传（自动检测） | `ZH-HANS` |
| 仅数字 / 标点 / 空白 | 不传（自动检测） | `ZH-HANS` |

| 编号 | 规格 |
| --- | --- |
| FR-LANG-1 | 判定阈值需可测试：Han 字符占比 ≥ 20% 且拉丁字母占比 < 20% → 中文；拉丁字母占比 ≥ 20% 且 Han 占比 < 20% → 英文；两者均 ≥ 20% → 混合。阈值在实现中定义为常量，便于调整。 |
| FR-LANG-2 | 短文本必须显式指定源语言，避免 DeepL 对单词/极短文本的自动检测不可靠。 |
| FR-LANG-3 | **修正**：DeepL API 没有 `source_lang = Auto` 这个取值，自动检测的语义是**省略 `source_lang` 参数**。 |
| FR-LANG-4 | 目标语言显式指定变体：英文 `EN-US`，中文 `ZH-HANS`（简体）。官方语言表中 `EN` 与 `ZH` 都是合法代码且**未被弃用**（`EN` 表示 all variants，`ZH` 表示 unspecified variant），但显式变体让输出可预期；同时 `EN-US` / `ZH-HANS` / `ZH-HANT` 只能作为目标语言。 |
| FR-LANG-5 | V1 不提供多语言模式设置，方向规则固定为：`中文 → 英文`、`英文 → 中文`、`其他/混合 → 中文`。源语言保持 `ZH` / `EN`，因为变体代码不能作为源语言。 |

### 4.8 DeepL 请求

| 编号 | 规格 |
| --- | --- |
| FR-API-1 | `POST {endpoint}/v2/translate`，`Content-Type: application/json`。 |
| FR-API-2 | 认证头：`Authorization: DeepL-Auth-Key <API_KEY>`。 |
| FR-API-3 | 请求体：`{"text": ["..."], "target_lang": "...", "source_lang": "..."（可选）}`。 |
| FR-API-4 | 默认 endpoint `https://api-free.deepl.com`（Free 账户）；设置中可切换 `https://api.deepl.com`（Pro）；高级设置允许自定义 endpoint。 |
| FR-API-5 | V1 不主动使用 `glossary`、`formality`、`context`、`custom_instructions`。 |
| FR-API-6 | 超时：连接 5s，读取 15s（`httpx.Timeout(15.0, connect=5.0)`），数值在实现阶段可微调。 |
| FR-API-7 | 超时/失败**不自动重试**，避免重复请求与意外消耗额度；V1 不做任何自动重试（原文档 §23）。注意：DeepL 官方对 `429`/`529`/`500`/`503`/`504` 建议指数退避重试，对 `400`/`403`/`404`/`413`/`456` 明确不建议重试——该差异已在 §12 Q7 确认为"V1 不重试"。 |
| FR-API-8 | 请求必须异步，UI 线程不得阻塞。 |
| FR-API-9 | 长度校验：官方单请求总大小上限为 **128 KiB（131072 字节）**，本地按 UTF-8 字节数校验；超限时本地提示 `Text too long`，不发送必然失败的请求。上限定义为常量以便随官方调整。 |
| FR-API-10 | 请求可取消：`Esc` 或重复提交时取消未完成的请求。 |
| FR-API-11 | 需处理的响应状态码（官方 `/v2/translate` 列出的完整集合）：`400`、`403`、`404`、`413`、`414`、`429`、`456`、`500`、`504`、`529`；虽然官方未列 `401`，客户端仍按未授权处理。 |
| FR-API-12 | 错误体解析：按状态码与 `code` 字段分支，**不匹配 `message` 文本**；同时兼容基础设施错误的 `{"error": {"message": …}}` 嵌套形态（读取 `body.message ?? body.error.message`）。 |
| FR-API-13 | 记录响应头 `X-Trace-ID` 到日志，便于向 DeepL 提工单。 |
| FR-API-14 | `model_type`（`quality_optimized` / `prefer_quality_optimized` / `latency_optimized`）V1 不启用，保持服务端默认值（§12 Q8 已确认）。 |

### 4.9 结果展示与复制

| 编号 | 规格 |
| --- | --- |
| FR-RESULT-1 | 结果区只显示最终译文，突出译文本身。 |
| FR-RESULT-2 | 不显示词典释义、词性、音标、多引擎对比、复杂语言选择。 |
| FR-RESULT-3 | 请求期间显示 Loading 态，禁用重复提交。 |
| FR-RESULT-4 | 结果显示后按 `Ctrl + C`：复制**仅译文文本**（不含 `Translation:` 等前缀），随后按设置关闭窗口。 |
| FR-RESULT-5 | `Close After Copy` 默认开启；关闭时只复制不关窗。 |
| FR-RESULT-6 | `Ctrl + C` 的拦截条件必须收窄：**仅在译文已显示且输入框内没有选中文本时**才解释为"复制译文"；否则保持系统默认复制行为。 |
| FR-RESULT-7 | 复制完成后可短暂显示 `已复制` 状态提示（不阻塞、不改变布局）。 |
| FR-RESULT-8 | 错误统一显示在同一窗口内，不弹系统级错误对话框。 |

### 4.10 设置

设置界面使用 libadwaita `PreferencesWindow`，V1 仅两页。

**General**

| 编号 | 规格 |
| --- | --- |
| FR-SET-1 | `Global Shortcut`：显示当前绑定；提供"更改"按钮（门户 `ConfigureShortcuts` 或 GSettings 路径）。 |
| FR-SET-2 | `Start on Login`：切换 `~/.config/autostart/deepl-quick-translate.desktop` 的创建/删除。 |
| FR-SET-3 | `Close After Copy`：默认开启。 |

**DeepL**

| 编号 | 规格 |
| --- | --- |
| FR-SET-4 | `API Key`：输入框为密码模式，回显 `••••••••abcd`（仅末 4 位）。 |
| FR-SET-5 | `Endpoint`：Free / Pro 两个预设，另有自定义 endpoint 的高级项。 |
| FR-SET-6 | `Test Connection`：调用 `GET /v2/usage` 验证 Key、Endpoint 与网络，**不消耗翻译额度**；成功显示 `Connection successful`，失败显示 `Connection failed`。 |
| FR-SET-7 | 设置不加入其它配置项。 |
| FR-SET-8 | 设置窗口入口：应用菜单启动 `deepl-quick-translate --settings`（由主实例接管并打开设置窗口）；翻译窗口右上角提供一个不干扰键盘流的小齿轮按钮（§12 Q3 已确认）。 |

### 4.11 生命周期

| 编号 | 规格 |
| --- | --- |
| FR-LIFE-1 | 启动后后台运行：注册快捷键 → 等待唤起，**不自动打开界面**。 |
| FR-LIFE-2 | 不依赖传统系统托盘。 |
| FR-LIFE-3 | 支持明确退出：`--quit` 或设置页 Quit。退出时注销全局快捷键、关闭门户会话、进程完全停止。 |
| FR-LIFE-4 | 因使用 `Gtk.Application`，需显式 `hold` 应用，使窗口全部关闭后进程仍常驻。 |

### 4.12 日志与隐私

| 编号 | 规格 |
| --- | --- |
| FR-PRIV-1 | 默认低日志级别（INFO），仅记录程序生命周期、快捷键注册状态。 |
| FR-PRIV-2 | **禁止记录**：API Key、用户翻译文本、剪贴板内容。 |
| FR-PRIV-3 | Debug 模式可额外记录：请求耗时、HTTP 状态码、程序生命周期细节；仍不记录完整翻译内容。 |
| FR-PRIV-4 | 默认输出到 stderr（便于 autostart / 终端排查），可选 `--log-file`。 |
| FR-PRIV-5 | 用户输入只发送给 DeepL，本地不做任何持久化：无翻译历史、无剪贴板历史、无云同步、无埋点。 |
| FR-PRIV-6 | 允许并建议记录响应头 `X-Trace-ID`（官方排障要求）；该值由 DeepL 生成，不含用户内容，不属于禁止记录项。 |

---

## 5. 架构与数据流

```
GlobalShortcut（门户 / GSettings）
        │  Activated
        ▼
TranslatorWindow ──────► ClipboardManager（仅唤起时读一次）
        │  Enter                 │
        ▼                        ▼
TranslationService ───────► 预填输入框
        │
        ▼
DeepLClient（httpx, async）
```

| 模块 | 职责 |
| --- | --- |
| `application.py` | `Gtk.Application` 子类：单实例、命令行参数路由、生命周期、门户会话持有 |
| `ui/translator_window.py` | 翻译窗口：输入、结果、快捷键处理、IME 守卫、错误展示 |
| `ui/settings_window.py` | libadwaita 设置界面 |
| `shortcuts/manager.py` | 门户注册/恢复/更换 + GSettings 兜底 |
| `clipboard/manager.py` | 只读 `text/plain`；写入译文并保持 selection |
| `deepl/client.py` | httpx 异步调用、超时、取消、错误映射 |
| `deepl/service.py` | 语言判定、长度校验、请求编排 |
| `config/manager.py` | 非敏感配置读写（JSON） |
| `config/secret.py` | Secret Service 存取 API Key |

---

## 6. 目录结构

```
deepl_tool/
├── app/
│   ├── __main__.py
│   ├── application.py
│   ├── ui/
│   │   ├── translator_window.py
│   │   └── settings_window.py
│   ├── deepl/
│   │   ├── client.py
│   │   └── service.py
│   ├── clipboard/
│   │   └── manager.py
│   ├── shortcuts/
│   │   └── manager.py
│   └── config/
│       ├── manager.py
│       └── secret.py
├── data/
│   ├── deepl-quick-translate.desktop
│   └── icons/
├── debian/                # .deb 打包
├── docs/
│   └── SPEC.md
├── tests/
├── run.sh                 # 开发直跑
└── README.md
```

不引入数据库。

---

## 7. 数据模型

**配置** `~/.config/<app_id>/config.json`（不含任何机密）

```json
{
  "version": 1,
  "endpoint": "https://api-free.deepl.com",
  "shortcut": {
    "backend": "portal",
    "preferred_trigger": "<Control><Alt>space",
    "gsettings_path": null,
    "command": "deepl-quick-translate --toggle"
  },
  "close_after_copy": true,
  "start_on_login": false,
  "hide_on_focus_loss": true,
  "log_level": "INFO"
}
```

`start_on_login` 默认为 `false`：首次运行不在用户同意前写入 autostart 文件，由用户在设置页显式开启（FR-SET-2）。

**凭据** Secret Service：service `<app_id>`，attribute `api_key`，仅存 DeepL API Key 明文于密钥环中。

---

## 8. 错误处理与用户可见文案

统一在翻译窗口内显示；主文案沿用需求文档的英文短语，第二行给中文说明。

| 触发条件 | 主文案 | 说明行 |
| --- | --- | --- |
| 401 / 403 | `Translation failed` | API Key 无效或缺少权限，请在设置中检查 |
| 400 | `Translation failed` | 请求参数无效（属程序缺陷，不应出现） |
| 404 / 414 | `Translation failed` | 收到意外的服务端响应 |
| 413（或本地校验超限） | `Text too long` | 文本超出 DeepL 单次上限（128 KiB） |
| 429 / 529 | `Translation failed` | 请求过于频繁，请稍后重试 |
| 456（额度用尽） | `Translation failed` | DeepL 额度已用完（Free 每月 50 万字符） |
| 500 / 503 / 504 | `Translation failed` | DeepL 服务暂时不可用 |
| 连接失败 / DNS 失败 | `Translation failed` | 无法连接 DeepL，请检查网络 |
| 读超时（15s） | `Translation timeout` | 请求超时，未自动重试 |
| 未配置 API Key | `API key required` | 请先在设置中填写 DeepL API Key |
| 快捷键注册失败 | `Shortcut unavailable` | 请到设置中更换快捷键 |

文案只由状态码与 `code` 决定，不依赖响应中的 `message` 文本。

---

## 9. 性能要求

| 编号 | 指标 |
| --- | --- |
| PERF-1 | 常驻进程内，"快捷键按下 → 窗口可见"目标 < 100 ms（不含合成器动画）。 |
| PERF-2 | 窗口首帧不得等待剪贴板读取、配置读取、密钥环读取或网络初始化。 |
| PERF-3 | 剪贴板读取、配置、密钥环均在窗口显示之后异步进行。 |
| PERF-4 | DeepL 的网络延迟不计入本指标，V1 不定义翻译完成时间上限。 |

---

## 10. 打包与安装

**产出**：`.deb`

**安装后布局**

```
/usr/bin/deepl-quick-translate
/usr/share/applications/<app_id>.desktop
/usr/share/icons/hicolor/scalable/apps/<app_id>.svg
/usr/share/doc/…（规格与说明）
```

**依赖**：`python3-gi`、`gir1.2-gtk-4.0`、`gir1.2-adw-1`、`python3-httpx`、`python3-secretstorage`

**用户流程**：下载 → 安装 → 启动 → 配置 API Key → 使用

**开发流程**：`git clone` → `./run.sh`（或 `python3 -m app`）

---

## 11. 验收标准（V1 完成）

**主链路 A：有剪贴板**

`Ctrl+Alt+Space` → 读取剪贴板 → 预填 → 修改 → `Enter` → DeepL → 显示译文 → `Ctrl+C` → 复制译文 → 自动关闭

**主链路 B：无剪贴板**

`Ctrl+Alt+Space` → 空输入框 → 输入中文/英文 → `Enter` → 正确方向翻译 → `Ctrl+C` → 自动关闭

**逐项测试清单**

| 编号 | 用例 | 预期 |
| --- | --- | --- |
| T1 | 冷启动后不打开窗口 | 进程常驻，无界面 |
| T2 | 首次按快捷键 | 门户确认流程完成后窗口出现并居中 |
| T3 | 二次唤起（含重启后） | 不再弹确认，直接出现窗口 |
| T4 | 剪贴板含 `How are you today?` → 唤起 | 输入框预填该文本 |
| T5 | 剪贴板含图片/文件 → 唤起 | 输入框为空 |
| T6 | 输入 `How are you?` → `Enter` | 发 `source_lang=EN, target_lang=ZH`，显示中文译文 |
| T7 | 输入 `你今天怎么样？` → `Enter` | 发 `source_lang=ZH, target_lang=EN-US`，显示英文译文 |
| T8 | 输入 `你好 hello` → `Enter` | 不传 `source_lang`，`target_lang=ZH` |
| T9 | fcitx5 拼音输入"你今天怎么样？" | 回车仅确认候选词，不触发翻译；再次回车才翻译 |
| T10 | 请求中再按 `Enter` | 被忽略，不产生第二次请求 |
| T11 | 请求中按 `Esc` | 窗口立即隐藏，响应到达后不更新任何 UI |
| T12 | 空输入按 `Enter` | 不发请求，窗口保持 |
| T13 | 显示译文后 `Ctrl+C` | 剪贴板为纯译文；窗口关闭；切到其它应用粘贴内容正确 |
| T14 | 关闭 `Close After Copy` 后 `Ctrl+C` | 只复制，不关窗 |
| T15 | 输入框内选中文本后 `Ctrl+C` | 复制选中文本，不触发"复制译文/关窗" |
| T16 | 再次执行 `./deepl-quick-translate` | 第二个进程退出，不产生第二个实例 |
| T17 | 设置中测试连接（正确 Key） | `Connection successful`，未消耗翻译额度 |
| T18 | 设置中测试连接（错误 Key） | `Connection failed` |
| T19 | 断开网络后翻译 | 显示翻译失败/超时，不自动重试 |
| T20 | 检查日志 | 不含 API Key、用户文本、剪贴板内容 |
| T21 | 设置中开启/关闭 Start on Login | autostart 文件被正确创建/删除 |
| T22 | `--quit` | 快捷键注销，进程完全退出 |
| T23 | 安装 `.deb` 后从应用菜单启动 | 正确名称、图标，单实例生效 |
| T24 | 额度耗尽（`456`） | 显示额度已用完文案，不重试 |
| T25 | 检查日志中的 `X-Trace-ID` | 存在且可复制；同时日志中仍无 API Key、用户文本、剪贴板内容 |

---

## 12. 开放问题（已全部确认）

2026-09-12 评审确认：Q1–Q8 全部采用建议默认值。

| # | 问题 | 结论 | 备注 |
| --- | --- | --- | --- |
| Q1 | 应用 ID / 程序名 / 中文名 | 采用 §2.1 的命名表 | 应用 ID `io.github.panwenzheng.DeepLQuickTranslate` |
| Q2 | 窗口失去焦点时是否自动隐藏 | 自动隐藏，可在设置中关闭 | 原需求文档未定义 |
| Q3 | 翻译窗口是否放极小的齿轮入口 | 放，不参与键盘流程 | — |
| Q4 | 错误文案语言 | 英文主文案 + 中文说明行（§8） | — |
| Q5 | 是否显示 DeepL 剩余额度 | V1 不显示，`/v2/usage` 仅用于连通性测试 | — |
| Q6 | 应用图标来源 | 先做简洁 SVG 占位，后续可重绘 | — |
| Q7 | 是否对 `429`/`529`/`500`/`503`/`504` 自动重试 | **不重试**，保持原文档 §23 的简单行为 | 与官方《Error handling》的退避重试建议不一致；若实际使用中偶发失败扰人，再引入"仅一次、指数退避并遵循 `Retry-After`"的窄重试 |
| Q8 | 是否使用 `model_type` 参数 | **不使用**，保持服务端默认 | `latency_optimized` 可能更快但牺牲质量 |

---

## 13. 里程碑

| 里程碑 | 范围 | 交付与验收 |
| --- | --- | --- |
| **M1 骨架** | 项目结构、`Gtk.Application` 单实例、ConfigManager、快捷键管理器（门户 + GSettings 兜底） | T1、T2、T3、T16、T22 |
| **M2 窗口与输入** | 无边框窗口、居中置前、焦点后读剪贴板、IME 守卫、Esc 行为 | T4、T5、T9、T12 |
| **M3 DeepL** | httpx 异步客户端、语言判定、超时、取消、错误映射 | T6、T7、T8、T10、T11、T19 |
| **M4 结果与设置** | 结果区、Ctrl+C 复制并关闭、Secret Service、设置窗口、Test Connection、Start on Login | T13–T15、T17、T18、T21 |
| **M5 打包** | `.deb`、`.desktop`、图标、README | T20、T23 |

---

## 14. 明确不做（V1）

本地词典、MDX/MDD、StarDict、GoldenDict、欧路词典、Query Router、多翻译引擎、LLM、OCR、TTS、翻译历史、云同步、浏览器插件、KDE、X11、系统托盘、SQLite。

V2 预留：本地词典 Provider、翻译历史、TTS、OCR、AI Rewrite、更多翻译服务——均不得影响 V1 核心体验。

---

## 附录 A · 与原需求文档的差异对照

以下为相对用户原始 50 节文档的**全部实质性改动**，逐条可追溯。

| 原节 | 原文档 | 本规格 | 原因 |
| --- | --- | --- | --- |
| §5 | 门户注册默认 `Ctrl+Alt+Space` | 门户 + GSettings 双路径；明确首次需用户按键确认 | GNOME 门户忽略 `preferred_trigger`，单一路径存在失败风险 |
| §7 | 显示 → 置前 → 获得焦点 → 读取剪贴板 | 保留，并升级为硬性实现约束 | Wayland 下未获得焦点无法读剪贴板 |
| §11 | 支持多行文本 + `Enter` 提交 | 明确 `Enter` 提交、`Shift+Enter` 换行 | 两者在键位语义上冲突，必须指定其一 |
| §12 | 输入法确认不应被误判为提交 | 保留，并补充实现要点与 T9 测试用例 | — |
| §16 / §17 | `source_lang = Auto` | **省略 `source_lang` 参数** | DeepL API 无 `Auto` 取值，自动检测即"不传该参数" |
| §15 / §17 | `target_lang = ZH` / `EN` | 英文目标 `EN-US`，中文目标 `ZH-HANS` | 官方语言表中 `EN`/`ZH` 均合法且未弃用（见附录 B）；改用显式变体是为让输出变体可预期，而变体代码只能作目标语言，故源语言仍为 `ZH`/`EN` |
| §23 | 默认不自动重试 | 保留不重试，但按状态码给出分级文案 | 官方建议对 429/5xx 退避重试；已确认 V1 不重试 |
| §20 | 未提及扩展参数 | 明确 `model_type` 等扩展参数 V1 不启用 | 见 FR-API-14；已确认不启用 |
| §30 | 按 DeepL 限制校验长度 | 量化为总请求 128 KiB，并处理 `413` | 官方《Usage and limits》 |
| §33 / §38 | 未定义错误解析与排障头 | 增加 `code` 分支、嵌套错误体兼容、`X-Trace-ID` 记录 | 官方《Error handling》明确要求 |
| §15 / §17 | §15 用"Latin alphabet"判英文，§17 又把 `Bonjour` 列为"其他语言" | 遵循 §15：拉丁字母为主即判为英文（`source_lang=EN`）。因此法语等拉丁字母文本会被标成 EN | 两节自相矛盾：无法在不引入词典的前提下区分 Bonjour 与 hello。若更看重"非英文拉丁文本"，可改为"较长的拉丁文本不指定源语言、交给 DeepL 自动检测" |
| §19 | 默认 endpoint 视账户而定 | 默认 `api-free.deepl.com`（用户确认为 Free） | 用户确认 |
| §26 | `Ctrl+C` 复制并关闭 | 保留，但补充拦截条件：仅当译文已显示且输入框无选中文本 | 否则会破坏输入框内的正常复制 |
| §26 / §37 | 复制后关闭窗口 | 明确为**隐藏窗口、进程保活** | Wayland 下进程退出后剪贴板内容丢失 |
| §30 | 按 DeepL 限制校验长度 | 量化为 128 KiB 单请求上限 | 给出可实现的确定值 |
| §33 | Test Connection 不实际翻译 | 明确用 `GET /v2/usage` 实现 | 该接口不消耗额度，最贴合原意 |
| §34 / §36 | Settings / Quit 入口后续再定 | 明确为 `--settings` / `--quit` 命令行入口，由主实例接管；翻译窗口提供小齿轮 | 无需托盘即可提供管理入口 |
| §41 | 建议 PySide6 | 改为 GTK4 + libadwaita | 用户同意；依赖为零、Wayland 与 IME 原生 |
| §42 | `main.py` + 无 settings 模块 | 用 `__main__.py`/`application.py`，新增 `ui/settings_window.py`、`config/secret.py` | 单实例由 Gtk.Application 承担；密钥与配置分离 |
| §47 | `.desktop` 支持单实例 | 保留；单实例由 GApplication 实现 | 更简洁可靠 |
| 全文 | 未定义窗口失焦行为、错误文案语言、图标 | 列为 §12 开放问题，其中 Q1–Q8 已确认采用默认值 | — |

---

## 附录 B · DeepL 官方文档核对结果

核对日期 2026-09-12，来源均为 `developers.deepl.com` 官方页面的 Markdown 源。

| 主题 | 官方结论 | 对本项目的影响 |
| --- | --- | --- |
| 认证 | 使用 `Authorization: DeepL-Auth-Key <key>`；`auth_key` 查询参数与 GET `/translate` 已于 2025 年弃用 | 与 §4.8 一致：POST + 认证头 |
| Endpoint | Pro `https://api.deepl.com`，Free `https://api-free.deepl.com` | 与 D9 一致 |
| 请求体 | `text`（数组，必填）、`target_lang`（必填）、`source_lang`（可选，**省略即自动检测**） | 确认 FR-LANG-3 的"省略 = 自动检测"写法正确 |
| 语言代码 | `EN` = English (all variants)、`ZH` = Chinese (unspecified variant)，**均未被弃用**；`EN-US`/`EN-GB`/`ZH-HANS`/`ZH-HANT` 是 variant，**只能作为目标语言** | 修正 v1.0 中"`EN` 已弃用"的**错误表述**；源语言 `ZH`/`EN`，目标语言 `ZH-HANS`/`EN-US` |
| 请求大小 | 总请求 128 KiB；请求头 16 KiB | 与 FR-API-9 一致 |
| Free 额度 | 500,000 字符/月，按源文本的 Unicode 码点计数 | 用于 §8 的 `456` 文案 |
| 用量接口 | `GET /v2/usage`；Free 账户只返回 `character_count` 与 `character_limit` | 用于 Test Connection（FR-SET-6） |
| 状态码 | `/v2/translate`：`400`/`403`/`404`/`413`/`414`/`429`/`456`/`500`/`504`/`529` | 见 FR-API-11 与 §8 |
| 错误体 | `{message, code}`；边缘/基础设施错误为 `{"error": {"message": …}}`；官方明确要求**不要匹配 `message` 文本** | 见 FR-API-12 |
| 排障头 | 响应头 `X-Trace-ID` 标识请求，官方要求默认记录 | 见 FR-PRIV-6 |
| 重试策略 | `429`/`529`/5xx 建议指数退避重试并遵循 `Retry-After`；`456`/`400`/`403`/`404`/`413` 不重试 | 与原文档 §23 冲突，已确认 V1 不重试 |
| 语言检测 | 官方明确：能指定源语言就应指定；单个词或极短句子的自动检测可能不可靠 | 支持原文档 §15 的做法 |
| 扩展参数 | `context`、`formality`、`glossary_id(s)`、`model_type`、`split_sentences`、`preserve_formatting`、`show_billed_characters`、`tag_handling` 等 | V1 均不使用（FR-API-5、FR-API-14） |
| 计费细节 | 源语言与目标语言相同时字符同样计费 | 不影响 V1（方向判定保证不会同语言互译） |

来源：

- https://developers.deepl.com/docs/getting-started/quickstart.md
- https://developers.deepl.com/api-reference/translate/request-translation.md
- https://developers.deepl.com/docs/resources/usage-limits.md
- https://developers.deepl.com/api-reference/usage-and-quota/check-usage-and-limits.md
- https://developers.deepl.com/docs/best-practices/error-handling.md
- https://developers.deepl.com/docs/getting-started/supported-languages.md
- https://developers.deepl.com/docs/best-practices/language-detection.md

---

## 附录 C · XDG 门户 GlobalShortcuts 实测结论

2026-09-12 在目标机器（Ubuntu 26.04 / GNOME 50.1 / Wayland）上用 `tools/portal_probe.py`
直接对 `org.freedesktop.portal.Desktop` 实测得到，非推测。

| # | 结论 | 证据 |
| --- | --- | --- |
| C1 | 门户确实提供 `GlobalShortcuts`，但本机 `version = 1`；公开文档描述的是 version 2（v2 才新增 `activation_token` 选项） | D-Bus introspection + 门户规范 |
| C2 | **必须有可识别的应用身份**：未安装 `<app_id>.desktop` 时，宿主注册失败并连带 `CreateSession` 被拒 | `Could not register app ID: App info not found for 'io.github.panwenzheng.DeepLQuickTranslate'`，随后 `NotAllowed: An app id is required` |
| C3 | 装好 `.desktop` 后宿主注册通过，`CreateSession` + `BindShortcuts` 正常，GNOME 弹出系统对话框要求用户按键确认 | `portal: host app registered as …`，用户按键后 `shortcuts bound: [('toggle-translator', {'description': '唤起翻译窗口', 'trigger_description': 'Press <Control><Alt>space'})]` |
| C4 | **确认只发生一次**：换新进程、重建会话后再次绑定同一 app id 的快捷键只需 0.3s，无任何对话框 | 同一探针连续两次运行，第二次 `real 0m0.306s` |
| C5 | 门户协议**没有** `restore_token` 字段（该机制属于屏幕共享 / 远程桌面门户）；"免二次确认"完全由 GNOME 后端按 app id 记忆实现 | 门户规范全文检索 `restore` 命中 0 次 |
| C6 | 宿主注册接口签名为 `Register(s app_id, a{sv} options)`，漏传 `options` 会导致调用失败 | D-Bus introspection |
| C7 | `Activated` 信号发在对象路径 `/org/freedesktop/portal/desktop` 上，**会话句柄是信号的第一个参数**，不是信号的对象路径；按 session 路径订阅将永远收不到通知 | 对照实验：按 session 路径订阅无任何回调，去掉路径/sender 过滤后立即收到 |
| C8 | `Activated` 的 options 里带 `activation_token`，应交给合成器（`Gdk.Toplevel.set_startup_id()`）以走正规激活路径 | 实测信号带 `{'activation_token': 'gnome-shell//5119-39-ubuntu_TIME…'}` |
| C9 | 走全局快捷键激活的应用，其**触发按键的松开事件可能永远送不到窗口**，GTK 会按系统重复率无限重复该键（实测 5 秒内 160+ 次），必须由应用侧守卫兜住 | 应用日志：`swallowed 163 leaked trigger key event(s) (user-typing)` |

对实现的影响：

- 门户路径**必须先确保 `.desktop` 已安装**，否则一律退化为 GSettings 兜底；
- 不需要、也不应实现任何恢复令牌的持久化逻辑；
- FR-SHORTCUT-3/4/10 与测试用例 T2/T3 已按上述实测结论改写。
