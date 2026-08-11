# Third-Party Notices

HeartBeat 使用了以下第三方软件。各自的许可证条款要求在此声明；完整的许可证文本可在各项目官方仓库或 pip 安装目录的 `*.dist-info/licenses/` 中找到。

## 直接依赖（requirements.txt）

| 组件 | 版本 | 许可证 | 说明 |
|---|---|---|---|
| PySide6 (Essentials/Addons) | 6.11.1 | **LGPL-3.0-only / GPL-2.0-only / GPL-3.0-only** | 本项目按 **LGPL-3.0** 条款以动态链接方式使用（Python 运行时 import），不修改 Qt 库本身。分发时必须：① 保留本声明；② 允许使用者替换 Qt 库文件重新链接（打包产物为 onedir 结构，Qt framework 位于 `HeartBeat.app/Contents/Frameworks/`，可直接替换后重签）。Qt 库许可证文本随 PySide6 安装包分发（`site-packages/PySide6/Qt` 下各 `LICENSES/` 目录） |
| fastembed | 0.8.0 | Apache-2.0 | 用于文本向量化。保留其版权声明与 NOTICE |
| sqlite-vec | 0.1.9 | MIT + Apache-2.0（捆绑 SQLite，public domain） | 向量索引扩展 |
| pyobjc-framework-Cocoa | 12.2.1 | MIT | macOS 状态栏托盘（替代 QSystemTrayIcon 的 workaround） |

## 间接依赖（pip 自动安装，随打包分发）

| 组件 | 版本 | 许可证 |
|---|---|---|
| onnxruntime | 1.28.0 | MIT |
| numpy | 2.5.2 | BSD-3-Clause（含 0BSD/MIT/Zlib/CC0 子组件） |
| Pillow | 12.3.0 | MIT-CMU（HPND 风格） |
| tokenizers | 0.23.1 | Apache-2.0 |
| huggingface_hub | 1.27.0 | Apache-2.0 |
| requests | 2.34.2 | Apache-2.0 |
| certifi | 2026.7.22 | MPL-2.0 |
| aiohttp / anyio / h11 / charset_normalizer / click / filelock / fsspec / pyyaml / flatbuffers | 最新 | MIT / BSD-3-Clause / Apache-2.0（详见各包 METADATA） |

## 构建工具（仅构建期，不随产物分发）

| 组件 | 许可证 | 说明 |
|---|---|---|
| PyInstaller | GPL-2.0-or-later **with bootloader exception** | 官方许可例外明确允许使用 PyInstaller 构建并分发任意程序（含商业/闭源），不传染 |

## 运行时下载的模型（首启从 HuggingFace 自动下载，不打包、不随源码分发）

fastembed 使用的 embedding 模型（如 BAAI/bge 系列等）由各自模型卡声明许可（多为 Apache-2.0 / MIT）。使用模型前请查阅对应 HuggingFace 模型卡的许可证条款。

## 许可合规说明

1. **本项目（HeartBeat）以 MIT 许可发布**，与上述所有第三方许可证兼容（LGPL 库可被任意程序动态链接）。
2. 若需获取上述任一组件的完整许可证文本，可通过 `pip download <包名> --no-deps` 或访问各项目 GitHub 仓库获取。
3. 模型文件的使用受各自模型卡许可约束，与 HeartBeat 的 MIT 许可相互独立。

— 本声明随 HeartBeat 源码与构建产物一同分发。
