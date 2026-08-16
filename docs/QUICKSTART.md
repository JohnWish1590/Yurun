# 快速开始（部署细节）

适用于想从源码跑起来、或自行打包分发的开发者。

## 1. 获取源码

```bash
git clone https://github.com/JohnWish1590/Yurun.git
cd Yurun
```

## 2. 环境

- Windows 10 / 11
- Python 3.12（建议；3.10+ 亦可）

## 3. 安装依赖

```powershell
pip install -r requirements.txt
```

## 4. 开发模式运行

```powershell
python src/main.py
# 或双击 run_yurun.bat（自动定位 python，可移植）
```

首次运行会在 `%APPDATA%\Yurun\` 生成 `config.json`，系统托盘出现语润图标。

## 5. 打包独立 EXE

```powershell
pyinstaller Yurun.spec --noconfirm
# 产物 dist/语润.exe（onefile，内联 prompts/ 与 assets/）
```

> 注：`--clean` 在部分开启安全删除拦截的环境会被拦截（内部删除被拦），可省略；旧构建残留用 `rm -rf build dist` 手动清理后重建。

## 6. 打包安装包（可选）

先有 `dist/语润.exe`，再：

```powershell
& "C:\Users\<你>\AppData\Local\Programs\Inno Setup 6\ISCC.exe" installer/yurun_setup.iss
# 产物 dist/语润-Setup-x.y.z.exe（含中文、桌面快捷方式、干净卸载）
```

## 7. 配置

右键托盘 → **设置**，填入两项 Key：

- **火山 SAUC Key**（语音识别，默认）
- **DeepSeek API Key**（润色，默认）

详见 `README.md` 第五、六节；想换更快的润色端点（火山方舟 / 阿里百炼），在设置里改 `api_base` / `api_model` 即可，代码无需改动。
