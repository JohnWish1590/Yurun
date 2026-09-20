# 发布说明规范

本文件说明 **语润 Yurun** 的版本号规则与发布流程，供协作者与自动化脚本遵循。

## 版本号

采用语义化版本 `主.次.修订`：

- **主版本**：重大架构 / 产品化变革（如从 Cindy 复刻到独立产品）。
- **次版本**：新增可感知功能（如单实例锁、Plan B 润色、短句直出）。
- **修订**：缺陷修复、文案、构建调整。

当前版本见 `src/logger.py` 的 `YURUN_VERSION` 与 `installer/yurun_setup.iss` 的 `MyAppVersion`，**两者必须保持一致**。

## 当前发布：v1.4.1（2026-09-20）

- 发布内容：仅修安装器与助手安装脚本。**主程序逻辑与 v1.4.0 完全一致**，v1.4.0 的全部功能（快捷键全自定义、纠错窗口、提权助手）原样保留。
- 修复 1：升级不再卡在「安装程序无法自动关闭所有应用程序」。安装器在 `PrepareToInstall` 里显式停掉高权限输入助手（`schtasks /End` + `taskkill /F`），不再依赖 Restart Manager —— 助手是无界面常驻进程，RM 只能发 `WM_CLOSE`、从不强杀，所以永远关不掉它，而它恰好占着安装包要替换的 `YurunInputHelper.exe`。
- 修复 2：`input_helper_setup.py::uninstall()` 原先只删登录任务。任务只负责**启动**助手，删任务不会结束已在跑的实例 → 卸载同样卡住。现在先 `/End` + `taskkill /F` 再删任务；卸载器另在 `CurUninstallStepChanged` 里加了同样的保险。
- 发布资产：`dist/语润-Setup-1.4.1.exe`（安装版）。GitHub Release 附件用 ASCII 副本 `Yurun-Setup-v1.4.1.exe` 与 `Yurun-v1.4.1.exe`。
- 行为变化：升级/卸载时安装器会静默停掉后台助手（约 1 秒 + 文件复制时间），装完由 `YurunHelperSetup.exe install` 重新注册并启动。用户**不需要**再手动去任务管理器结束 `YurunInputHelper.exe`。
- 回退点：Git 标签 `v1.4.0`；本地目录 `dist_old_v1.4.0_20260920/` 保留 v1.4.0 全部产物。

### 历史（v1.4.0）

- v1.4.0 是自 v1.3.3 之后的第一个正式 Release；v1.3.1、v1.3.2、v1.3.4 只打过 Git 标签、没有建过 GitHub Release。

## 发布流程（手动）

1. 在 `src/logger.py` 与 `installer/yurun_setup.iss` 同步更新版本号。
2. 在 `CHANGELOG.md` 顶部按既有格式追加 `[x.y.z]` 条目（编辑 / 涉及文件 / 背景 / 改动点 / 验证 / 行为变化）。
3. 构建（Windows，用**系统 Python 3.12 + PyInstaller 6.x**；托管 3.13 打出来只有 ~28MB 的残缺产物，启动即崩，正确体积是 68MB 量级）：
   - `python -m PyInstaller Yurun.spec --noconfirm` → 生成 `dist/语润.exe`、`dist/YurunInputHelper.exe`、`dist/YurunHelperSetup.exe`。
     - **不要加 `--clean`**；`--noconfirm` 会删除同名旧产物，动手前先把 `dist/` 里上一版产物改名让开。
   - 用 Inno Setup 构建安装包 `dist/语润-Setup-x.y.z.exe`：
     `"<Inno Setup 7 安装目录>\ISCC.exe" installer\yurun_setup.iss`
     - 必须用 **Inno Setup 7**：6 的 `Languages\` 目录里没有 `ChineseSimplified.isl`，会在解析 `[Languages]` 时直接中止（engine 6.7.3 实测失败，7.1.0 通过）。
   - ⚠️ 安装器会在 `PrepareToInstall` 里**先停掉高权限输入助手**（`schtasks /End` + `taskkill /IM YurunInputHelper.exe /F`，见 `yurun_setup.iss` 的 `[Code] StopInputHelper`）。**不要**把它换成依赖 Restart Manager 的自动关闭：助手是无界面常驻进程，RM 只能给顶层窗口发 `WM_CLOSE`、从不强杀，所以永远关不掉它，而它占着 `YurunInputHelper.exe` —— v1.4.0 就是这样每次升级都弹「安装程序无法自动关闭所有应用程序」的。
4. 本地冒烟测试安装版（启动 banner 版本号、托盘图标、热键、单次录音、个人记忆窗口）；若包含助手，再验证普通启动的语润可向一个高权限测试程序输入。
   - 至少要跑一次冻结产物启动冒烟：启动 `dist\语润.exe`，确认日志里依次出现 `语润 vX.Y.Z 启动`、`已连接高权限输入助手（能力: …）`、`系统热键已启用`、`纠错热键监听已启动`、`托盘图标已提交`。
5. 提交源码，打标签 `git tag vx.y.z`，推送 `main` 与标签（用 token-in-URL 直连，本机没有 gh CLI 凭据）。
6. 在 GitHub 创建 Release `vx.y.z`，正文贴 CHANGELOG 对应片段，附件**必须使用 ASCII 文件名**（中文名会被 GitHub 解析成 `default.exe`）：`Yurun-Setup-vX.Y.Z.exe`（安装包）与 `Yurun-vX.Y.Z.exe`（便携主程序）。
   - 用 `tools/github_release.py` 一步完成建 Release + 上传附件（token 只从环境变量读，不落盘）：
     ```
     set GH_TOKEN=ghp_xxx
     python tools\github_release.py v1.4.0 "语润 v1.4.0 — <标题>" release_body_v1.4.0.md dist\Yurun-Setup-v1.4.0.exe dist\Yurun-v1.4.0.exe
     ```
     脚本可重复执行：已存在的 Release 会复用，已上传的附件会跳过。
   - 上传前先把中文名产物复制/改名为 ASCII 名（内容完全相同，不必重新打包）。

## 从 Preview 提升到正式版

Preview 区在 `Pre/`（整体不进 Git），正式版是仓库根的 `src/`。提升是一次**单独授权**的动作，不能顺手并入。

1. 打回退标签：`git tag stable-before-vX.Y.Z-promotion-YYYYMMDD`（在提升**之前**打）。
2. 把 `dist/` 里上一版产物改名让开（`--noconfirm` 会删同名文件），最好连同被覆盖的 `src/` 旧文件一起备份到 `dist_old_*/`。
3. 只把**内容不同**的 `Pre/src/*.py` 覆盖到 `src/`，然后核对 `src/` 与 `Pre/src/` 是否已经**完全一致** —— 一致即表示预览区已重置到新基线，下一轮开发从干净状态开始。
4. 同步版本号（两处必须一致，注意 `.iss` 里是 `#define MyAppVersion "x.y.z"`，**没有等号**）：
   - `src/logger.py` 的 `YURUN_VERSION`
   - `installer/yurun_setup.iss` 的 `MyAppVersion`
5. 版本号规则：新增可感知功能 → 次版本 +1；仅缺陷修复 / 文案 / 构建调整 → 修订 +1。
6. 若本次改动了 `src/privileged_helper.py`，安装包里的 `YurunInputHelper.exe` 会随之更新，安装时由 `YurunHelperSetup.exe install` 替换 `C:\Program Files\语润\YurunInputHelper.exe`，旧文件会留下一份 `.bak-<时间戳>`。助手协议 `PROTOCOL_VERSION` 保持**纯增量**（老助手缺新能力时由 `capabilities` 协商降级）。

## 说明

- 源码仓库不含 `dist/`、`build/`（已在 `.gitignore` 排除），打包产物仅通过 GitHub Release 附件分发。
- 旧 v0.1.1 历史保留在 `legacy-v0.1.1` 标签，供回溯。
- 备份目录命名统一为 `dist_old_*/`，已在 `.gitignore` 的 `dist_*/` 规则里排除。
