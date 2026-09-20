# 发布说明规范

本文件说明 **语润 Yurun** 的版本号规则与发布流程，供协作者与自动化脚本遵循。

## 版本号

采用语义化版本 `主.次.修订`：

- **主版本**：重大架构 / 产品化变革（如从 Cindy 复刻到独立产品）。
- **次版本**：新增可感知功能（如单实例锁、Plan B 润色、短句直出）。
- **修订**：缺陷修复、文案、构建调整。

当前版本见 `src/logger.py` 的 `YURUN_VERSION` 与 `installer/yurun_setup.iss` 的 `MyAppVersion`，**两者必须保持一致**。

## 当前发布：v1.4.0（2026-09-20）

- 发布内容：从 Preview 验收并入「输入控制」整轮能力。录音热键与纠错热键各自独立、都可带修饰键自定义；选中文字按热键弹出「错误纠正」窗口（默认 Alt + `）；设置界面新增热键录制控件与「录音 / 纠错」槽位切换；热键注册改用 `RegisterHotKey`，并修掉进程级窗口过程、跨线程销毁窗口、特殊键虚拟键码这三个热键真 bug；纠错在 Cindy、WorkBuddy 等以管理员权限运行的软件里也能读到选区。
- 发布边界：不包含暂停中的 TSF 输入法实验、常驻麦克风、pre-roll、Partial 直接上屏、自动学习键盘内容或焦点策略实验。拼音候选栏仍由用户现有输入法绘制，不被语润修改。
- 兼容性：旧配置缺少 `hotkey_modifiers` / `correction_hotkey_modifiers` 时按默认补齐（录音 = 裸反引号，纠错 = Alt + 反引号），升级后手感与原版本一致；纠错热键由 Ctrl + ` 改为 Alt + `（Cindy 侧边栏占用了前者）。
- 发布资产：`dist/语润.exe`、`dist/YurunInputHelper.exe`、`dist/YurunHelperSetup.exe` 共同组成安装版运行组件；用户分发使用 `dist/语润-Setup-1.4.0.exe`。GitHub Release 附件使用 ASCII 副本 `Yurun-Setup-v1.4.0.exe` 与 `Yurun-v1.4.0.exe`。
- 权限行为：安装时一次性创建登录后的高权限输入助手任务；主程序日常以普通权限运行。卸载器会先删除该任务，再移除程序和 `%APPDATA%\\Yurun` 数据。
- 回退点：Git 标签 `stable-before-v1.4.0-promotion-20260920` 指向并入前的 v1.3.4 稳定版本；本地目录 `dist_old_v1.3.4_20260920/` 保留 v1.3.4 的全部打包产物与提升前的源码副本。
- 遗留说明：v1.3.1、v1.3.2、v1.3.4 只打过 Git 标签，没有建过 GitHub Release（因此 v1.3.4 的产物只存在于上一行那个本地目录里）。v1.4.0 是自 v1.3.3 之后的第一个正式发布。

## 发布流程（手动）

1. 在 `src/logger.py` 与 `installer/yurun_setup.iss` 同步更新版本号。
2. 在 `CHANGELOG.md` 顶部按既有格式追加 `[x.y.z]` 条目（编辑 / 涉及文件 / 背景 / 改动点 / 验证 / 行为变化）。
3. 构建（Windows，用**系统 Python 3.12 + PyInstaller 6.x**；托管 3.13 打出来只有 ~28MB 的残缺产物，启动即崩，正确体积是 68MB 量级）：
   - `python -m PyInstaller Yurun.spec --noconfirm` → 生成 `dist/语润.exe`、`dist/YurunInputHelper.exe`、`dist/YurunHelperSetup.exe`。
     - **不要加 `--clean`**；`--noconfirm` 会删除同名旧产物，动手前先把 `dist/` 里上一版产物改名让开。
   - 用 Inno Setup 构建安装包 `dist/语润-Setup-x.y.z.exe`：
     `"<Inno Setup 7 安装目录>\ISCC.exe" installer\yurun_setup.iss`
     - 必须用 **Inno Setup 7**：6 的 `Languages\` 目录里没有 `ChineseSimplified.isl`，会在解析 `[Languages]` 时直接中止（engine 6.7.3 实测失败，7.1.0 通过）。
4. 本地冒烟测试安装版（启动 banner 版本号、托盘图标、热键、单次录音、个人记忆窗口）；若包含助手，再验证普通启动的语润可向一个高权限测试程序输入。
   - 至少要跑一次冻结产物启动冒烟：启动 `dist\语润.exe`，确认日志里依次出现 `语润 vX.Y.Z 启动`、`已连接高权限输入助手（能力: …）`、`系统热键已启用`、`纠错热键监听已启动`、`托盘图标已提交`。
5. 提交源码，打标签 `git tag vx.y.z`，推送 `main` 与标签（用 token-in-URL 直连，本机没有 gh CLI 凭据）。
6. 在 GitHub 创建 Release `vx.y.z`，正文贴 CHANGELOG 对应片段，附件**必须使用 ASCII 文件名**（中文名会被 GitHub 解析成 `default.exe`）：`Yurun-Setup-vX.Y.Z.exe`（安装包）与 `Yurun-vX.Y.Z.exe`（便携主程序）。

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
