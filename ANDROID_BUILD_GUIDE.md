# GitHub Actions Android Buildozer 终极指南

这份指南凝聚了我们在 CI 环境下使用 Buildozer 踩过的所有坑。它提供了一套经过验证的**“黄金模板”**，直接适用于任何新的 Kivy/Python-for-Android 项目。

---

## 🚀 核心架构设计

为了避开 GitHub Actions 的各种限制（Root 权限、用户交互、文件权限），我们采用以下架构：

1.  **权限接管**：不使用 Docker 内部的存取机制，而是在宿主机（Runner）上预先创建好所有缓存目录（`.buildozer`, `.android`, `.gradle`, `.kivy`）并赋予 `777` 权限。
2.  **身份伪装**：通过 `--user` 参数，让 Docker 容器以 Runner 的普通用户身份运行，彻底规避 Root 检查。
3.  **环境隔离**：放弃容器内自带的 Python 环境，在挂载的工作目录下**自建 venv**，确保 pip 拥有完全的读写权限。
4.  **自动应答**：使用 `yes | command` 自动处理所有许可证（License）确认。
5.  **持久缓存**：利用 `actions/cache` 对上述目录进行云端缓存，实现**增量编译**（从 20 分钟缩短至 2 分钟）。

---

## 🛠️ 黄金工作流模板 (`build.yml`)

将此文件放入你仓库的 `.github/workflows/build.yml`。**开箱即用，无需修改 Docker 镜像。**

```yaml
name: Build Android APK
on:
  push:
    branches: [ main ]
  workflow_dispatch:

jobs:
  build:
    runs-on: ubuntu-latest
    
    steps:
      # 1. 检出代码
      - name: Checkout code
        uses: actions/checkout@v4

      # 2. 启用缓存（极速构建的关键）
      - name: Cache Buildozer
        uses: actions/cache@v4
        with:
          path: |
            ~/.buildozer
            ~/.android
            ~/.gradle
            ~/.kivy
          key: buildozer-${{ runner.os }}-${{ hashFiles('buildozer.spec') }} # 依赖变动时才重置缓存
          restore-keys: |
            buildozer-${{ runner.os }}-

      # 3. 执行构建（核心黑科技）
      - name: Build with Buildozer Docker
        run: |
          # === 准备工作：权限大赦 ===
          # 在宿主机创建所有缓存目录，确保持久化和可写
          mkdir -p $HOME/.buildozer $HOME/.android $HOME/.gradle $HOME/.kivy
          chmod -R 777 . $HOME/.buildozer $HOME/.android $HOME/.gradle $HOME/.kivy

          # 获取当前用户 ID，用于欺骗 Docker
          USER_ID=$(id -u)
          GROUP_ID=$(id -g)
          
          # === 启动容器 ===
          # --user: 以宿主机用户身份运行，绕过 buildozer root 检查
          # --entrypoint /bin/sh: 无视官方可能有问题的启动脚本
          # -v: 挂载所有缓存目录到容器内对应位置
          docker run --rm \
            --user $USER_ID:$GROUP_ID \
            --entrypoint /bin/sh \
            -v "$(pwd)":/home/user/hostpython \
            -v "$HOME/.buildozer":/home/user/.buildozer \
            -v "$HOME/.android":/home/user/.android \
            -v "$HOME/.gradle":/home/user/.gradle \
            -v "$HOME/.kivy":/home/user/.kivy \
            -e REPO_PATH=/home/user/hostpython \
            -e HOME=/home/user \
            -e GRADLE_USER_HOME=/home/user/.gradle \
            kivy/buildozer \
            -c "
            # === 容器内部脚本 ===
            
            # 1. 环境隔离：在挂载的目录下自建 venv (解决 pip 权限问题)
            cd /home/user/hostpython
            python3 -m venv myenv
            . myenv/bin/activate
            
            # 2. 安装必要依赖
            # 指定 Cython 版本以兼容旧版 Kivy
            pip install --upgrade pip
            pip install buildozer cython==0.29.36 appdirs 'colorama>=0.3.3' jinja2 'sh>=1.10,<2.0' build toml packaging setuptools
            
            # 3. 开始编译
            # export USE_CCACHE=0: 禁用可能损坏的编译缓存
            # yes | ...: 自动同意 Android SDK 协议
            cd android  # 如果你的 spec 文件在根目录就不需要这行
            export USE_CCACHE=0
            yes | buildozer android debug
            "

      # 4. 上传产物
      - name: Upload APK
        uses: actions/upload-artifact@v4
        with:
          name: app-release
          path: android/bin/*.apk  # 根据你的实际输出路径调整
```

---

## 🐍 代码层面的防守：Crash Handler

由于我们看不到 CI 构建出来的包在手机上的报错，建议在 Python 入口文件 (`main.py`) 加上这个**防闪退机制**。它能把闪退变成屏幕上的报错信息，极大地方便调试。

```python
from kivy.app import App
from kivy.lang import Builder
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.scrollview import ScrollView
from kivy.core.window import Window

# 你的 App 类定义...
class MyApp(App):
    pass

if __name__ == '__main__':
    try:
        MyApp().run()
    except Exception:
        # === 崩溃捕获器 ===
        # 捕捉 Traceback 并显示在屏幕上，而不是直接闪退
        import traceback
        err = traceback.format_exc()
        
        class CrashApp(App):
            def build(self):
                # 简单的报错显示界面
                scroll = ScrollView()
                label = Label(text=f"CRASH REPORT:\n\n{err}", 
                              size_hint_y=None, 
                              text_size=(Window.width * 0.95, None),
                              halign='left', valign='top')
                label.bind(texture_size=label.setter('size'))
                scroll.add_widget(label)
                return scroll
                
        CrashApp().run()
```

---

## ❓ 常见报错速查

| 报错关键词 | 原因 | 解决方案 |
| :--- | :--- | :--- |
| `Run as root!` / `[y/n]?` | Docker 默认为 Root，Buildozer 会暂停确认 | 使用 `--user $USER_ID` 参数运行 Docker |
| `Accept? (y/N)` | Android SDK 协议未同意 | 使用 `yes \| buildozer ...` |
| `Permission denied: .buildozer` | 容器内无权创建目录 | 宿主机预先 `mkdir` 并 `chmod 777`，且必须挂载 |
| `pip install ... denied` | 容器自带 Python 属于 Root | 使用 `python3 -m venv` 自建虚拟环境 |
| `cannot compute suffix` | NDK 编译环境脏了 | 手动执行一次 `rm -rf .buildozer` (慎用，会清除缓存) |
| `config.pxi not found` | Kivy 无法写入 .kivy 配置 | 挂载并放开 `.kivy` 目录权限 |
