# 发布说明规范

本文件说明 **语润 Yurun** 的版本号规则与发布流程，供协作者与自动化脚本遵循。

## 版本号

采用语义化版本 `主.次.修订`：

- **主版本**：重大架构 / 产品化变革（如从 Cindy 复刻到独立产品）。
- **次版本**：新增可感知功能（如单实例锁、Plan B 润色、短句直出）。
- **修订**：缺陷修复、文案、构建调整。

当前版本见 `src/logger.py` 的 `YURUN_VERSION` 与 `installer/yurun_setup.iss` 的 `MyAppVersion`，**两者必须保持一致**。

## 当前发布：v1.3.1（2026-08-30）

- 发布内容：在 v1.3.0 快速输入基础上，正式纳入纠错后立即替换选中文字、独立 API Key 确认、热键即时生效、设置界面重设计与记忆位置、更新的程序/托盘图标，以及浮窗定位锚点修复。
- 发布边界：焦点策略实验未合入、未打包、未发布；新的浮窗反馈动效将从 v1.3.1 基线另开 Preview 开发。
- 发布资产：`dist/语润.exe` 为本机正式包；GitHub 附件使用 ASCII 文件名 `Yurun-v1.3.1.exe`，避免平台错误解析中文文件名。
- 回退点：`v1.3.0` 标签及 `dist/语润-v1.3.0-before-v1.3.1.exe`。

## 发布流程（手动）

1. 在 `src/logger.py` 与 `installer/yurun_setup.iss` 同步更新版本号。
2. 在 `CHANGELOG.md` 顶部按既有格式追加 `[x.y.z]` 条目（编辑 / 涉及文件 / 背景 / 改动点 / 验证 / 行为变化）。
3. 构建：
   - `pyinstaller Yurun.spec --noconfirm` → 生成 `dist/语润.exe`。
   - 用 Inno Setup 构建安装包 `dist/语润-Setup-x.y.z.exe`。
4. 本地冒烟测试 `dist/语润.exe`（启动 banner 版本号、托盘图标、热键、单次录音润色）。
5. 提交源码，打标签 `git tag vx.y.z`，推送 `master` 与标签。
6. 在 GitHub 创建 Release `vx.y.z`，正文贴 CHANGELOG 对应片段，附件挂 `语润.exe` 与安装包。

## 说明

- 源码仓库不含 `dist/`、`build/`（已在 `.gitignore` 排除），打包产物仅通过 GitHub Release 附件分发。
- 旧 v0.1.1 历史保留在 `legacy-v0.1.1` 标签，供回溯。
