# 发布说明

本文件记录语润的版本规则、当前发布内容和可复现的 Windows 发布流程。开发过程中的方案讨论、试验和反复见 [`docs/开发过程-思考尝试与反复.md`](docs/开发过程-思考尝试与反复.md)，每个版本的细节见 [`CHANGELOG.md`](CHANGELOG.md)。

## 版本规则

采用语义化版本 `主.次.修订`：

- 主版本：重大架构或产品化变革；
- 次版本：新增用户可感知功能；
- 修订版本：缺陷修复、文案和构建调整。

每次重新打包都必须递增版本号。以下两处必须一致：

- `src/logger.py` → `YURUN_VERSION`
- `installer/yurun_setup.iss` → `MyAppVersion`

## 当前发布：v1.4.4（2026-09-21）

1. 修复纠错窗口无法自动读取选中文字的问题：保留安全的 `CF_DIB/CF_DIBV5`，继续跳过可能导致堆损坏的 `CF_BITMAP` 和应用私有格式。
2. 保留 1.4.3 的原生剪贴板安全保护，避免再次触发纠错窗口崩溃。
3. 安装器为开始菜单和桌面快捷方式显式绑定语润图标。
4. 升级时自动结束旧版主程序和高权限输入助手，不要求用户手动关闭旧版本。

普通用户只需要下载一个文件：

`Yurun-Setup-v1.4.4.exe`

安装器会自动安装主程序、高权限输入助手和助手安装组件；`YurunInputHelper.exe`、`YurunHelperSetup.exe` 不需要单独下载。

## 构建

环境：Windows、系统 Python 3.12、PyInstaller 6.x、Inno Setup 7。

```powershell
python -m compileall -q src tests
python -m unittest discover -s tests -p 'test_*.py' -v
python -m PyInstaller --noconfirm Yurun.spec
& "$env:LOCALAPPDATA\Programs\Inno Setup 7\ISCC.exe" installer\yurun_setup.iss
```

生成的内部文件为：

- `dist/语润.exe`
- `dist/YurunInputHelper.exe`
- `dist/YurunHelperSetup.exe`

发布给用户的文件只有安装包。`build/`、`dist/`、`__pycache__/` 是本地生成物，均不提交到 Git。

安装器的 `PrepareToInstall` 会先执行：

- `schtasks /End /TN "Yurun Input Helper"`
- `taskkill /IM YurunInputHelper.exe /F`
- `taskkill /IM "语润.exe" /F`

因此可以在旧版仍运行时直接升级；安装完成后会重新注册并启动助手。

## 发布到 GitHub

1. 确认测试和构建通过。
2. 提交 `main` 并创建版本标签：

   ```powershell
   git add -A
   git commit -m "release: v1.4.4"
   git tag v1.4.4
   git push origin main --tags
   ```

3. 创建 GitHub Release `v1.4.4`，只上传一个 ASCII 文件名的安装包：

   `Yurun-Setup-v1.4.4.exe`

中文文件名可以保留在本地，但 GitHub Release 使用 ASCII 文件名，避免被解析成 `default.exe`。

## 回退

历史版本和回退点保留在 Git 标签中；不要把旧 EXE、旧 `build/` 或旧 `dist/` 目录重新放回源码仓库。完整的历史决策与被否决的尝试见开发过程记录。
