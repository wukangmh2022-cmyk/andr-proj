import subprocess
import os

# --- 配置 ---
VIDEO_INPUT = "1_with_new_audio.mp4"
SUBTITLE_INPUT = "1_原文.srt"
VIDEO_OUTPUT = "1_with_subtitles.mp4"

# FFmpeg 样式 (ASS 格式)
# Alignment=2 (底部居中)
# PrimaryColour=&HFFFFFF& (白色)
# OutlineColour=&H000000& (黑色)
# Outline=2 (2px 描边)
# Shadow=0 (无阴影)
STYLE_STRING = "Alignment=2,PrimaryColour=&HFFFFFF&,OutlineColour=&H000000&,Outline=2,Shadow=0"
# --- 结束配置 ---

def escape_path_for_ffmpeg_filter(path_str):
    """
    为 FFmpeg 的 -vf filter (libass) 正确转义路径，
    主要处理 Windows 的 'C:\' 冒号问题。
    """
    if os.name == 'nt': # 如果是 Windows
        # 将 C:\path\to.srt 转换为 C\:/path/to.srt
        path_str = path_str.replace('\\', '/')
        if ':' in path_str:
            drive, rest = path_str.split(':', 1)
            return f"{drive}\\:{rest}"
        return path_str
    else: # macOS/Linux
        # Linux/macOS 路径通常没问题
        return path_str

def add_subtitles():
    """
    调用 FFmpeg 将字幕烧录到视频中。
    """
    print("--- 开始添加字幕任务 ---")

    # 1. 检查输入文件是否存在
    if not os.path.exists(VIDEO_INPUT):
        print(f"❌ 错误: 找不到视频文件 '{VIDEO_INPUT}'")
        return
    if not os.path.exists(SUBTITLE_INPUT):
        print(f"❌ 错误: 找不到字幕文件 '{SUBTITLE_INPUT}'")
        return
    
    print(f"视频输入: {VIDEO_INPUT}")
    print(f"字幕输入: {SUBTITLE_INPUT}")

    # 2. 准备 FFmpeg 命令
    
    # [关键] 必须转义字幕文件路径，以供 FFmpeg 滤镜正确读取
    # 我们使用 os.path.abspath 来获取完整路径，然后转义
    # 即使是相对路径 '1.srt'，转为绝对路径再转义也更安全
    abs_subtitle_path = os.path.abspath(SUBTITLE_INPUT)
    escaped_subtitle_path = escape_path_for_ffmpeg_filter(abs_subtitle_path)
    
    # 构造滤镜 (-vf) 字符串
    filter_vf = f"subtitles=filename='{escaped_subtitle_path}':force_style='{STYLE_STRING}'"

    command = [
        "ffmpeg",
        "-y",                   # 覆盖已存在的输出文件
        "-i", VIDEO_INPUT,      # 输入视频
        "-c:a", "copy",         # 直接复制音频流（不重编码）
        "-c:v", "libx264",      # 重新编码视频以烧录字幕
        "-preset", "fast",      # 使用 'fast' 预设以加快速度
        "-crf", "23",           # 视觉质量 (18-28 是合理范围)
        "-vf", filter_vf,       # 应用字幕滤镜和样式
        VIDEO_OUTPUT            # 输出文件
    ]

    print("\n[正在执行 FFmpeg 命令]:")
    # 打印一个易于调试的命令版本
    print(" ".join(f'"{arg}"' if ' ' in arg or ':' in arg else arg for arg in command))

    # 3. 执行命令
    try:
        # 使用 check=True，如果 FFmpeg 失败，Python 会抛出异常
        # text=True 使输出为文本格式
        subprocess.run(command, check=True, text=True, capture_output=True)
        
        print(f"\n🎉 处理完成！")
        print(f"输出文件: {VIDEO_OUTPUT}")

    except subprocess.CalledProcessError as e:
        print(f"\n❌ FFmpeg 执行失败。")
        print(f"返回代码: {e.returncode}")
        print("--- FFmpeg 错误输出 ---")
        print(e.stderr)
        print("------------------------")
    except FileNotFoundError:
        print("\n❌ 错误: 找不到 'ffmpeg' 命令。")
        print("请确保 FFmpeg 已安装，并且其路径已添加到您的系统 PATH 环境变量中。")
    except Exception as e:
        print(f"\n❌ 发生意外错误: {e}")

if __name__ == "__main__":
    add_subtitles()