# 发布说明规范

本文件说明 **语润 Yurun** 的版本号规则与发布流程，供协作者与自动化脚本遵循。

## 版本号

采用语义化版本 `主.次.修订`：

- **主版本**：重大架构 / 产品化变革（如从 Cindy 复刻到独立产品）。
- **次版本**：新增可感知功能（如单实例锁、Plan B 润色、短句直出）。
- **修订**：缺陷修复、文案、构建调整。

当前版本见 `src/logger.py` 的 `YURUN_VERSION` 与 `installer/yurun_setup.iss` 的 `MyAppVersion`，**两者必须保持一致**。

## 当前发布：v1.3.4（2026-09-04）

- 发布内容：从 Preview 验收并入浮窗定位回退和长文本动画收尾修复。针对 WorkBuddy、Codex 等 Electron / Chromium 界面，在无法读取 caret 或 UIA 输入控件时，浮窗固定使用热键按下瞬间的位置；长文本动画依据已确认输入的字符进度收尾，并仅略早于最终文字结束。
- 发布边界：不包含暂停中的 TSF 输入法实验、常驻麦克风、pre-roll、Partial 直接上屏、自动学习键盘内容或焦点策略实验。拼音候选栏仍由用户现有输入法绘制，不被语润修改。
- 发布资产：`dist/语润.exe`、`dist/YurunInputHelper.exe`、`dist/YurunHelperSetup.exe` 共同组成安装版运行组件；用户分发使用 `dist/语润-Setup-1.3.4.exe`。GitHub Release 附件使用 ASCII 副本 `Yurun-Setup-v1.3.4.exe`。
- 权限行为：安装时一次性创建登录后的高权限输入助手任务；主程序日常以普通权限运行。卸载器会先删除该任务，再移除程序和 `%APPDATA%\\Yurun` 数据。
- 回退点：Git 标签 `stable-before-v1.3.4-promotion-20260904` 指向并入前的 v1.3.3 稳定版本。

## 发布流程（手动）

1. 在 `src/logger.py` 与 `installer/yurun_setup.iss` 同步更新版本号。
2. 在 `CHANGELOG.md` 顶部按既有格式追加 `[x.y.z]` 条目（编辑 / 涉及文件 / 背景 / 改动点 / 验证 / 行为变化）。
3. 构建：
   - `pyinstaller Yurun.spec --noconfirm` → 生成 `dist/语润.exe`、`dist/YurunInputHelper.exe`、`dist/YurunHelperSetup.exe`。
   - 用 Inno Setup 构建安装包 `dist/语润-Setup-x.y.z.exe`。
4. 本地冒烟测试安装版（启动 banner 版本号、托盘图标、热键、单次录音、个人记忆窗口）；若包含助手，再验证普通启动的语润可向一个高权限测试程序输入。
5. 提交源码，打标签 `git tag vx.y.z`，推送 `master` 与标签。
6. 在 GitHub 创建 Release `vx.y.z`，正文贴 CHANGELOG 对应片段，附件挂 `语润.exe` 与安装包。

## 说明

- 源码仓库不含 `dist/`、`build/`（已在 `.gitignore` 排除），打包产物仅通过 GitHub Release 附件分发。
- 旧 v0.1.1 历史保留在 `legacy-v0.1.1` 标签，供回溯。
