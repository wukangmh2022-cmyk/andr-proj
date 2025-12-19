import subprocess
import os
import shutil

# --- 配置 ---
VIDEO_INPUT = "1_with_subtitles.mp4"
VIDEO_OUTPUT = "1_trimmed_output.mp4"

# 剪切区间 (要去除的时间段)
# 格式: 'HH:MM:SS' 或 'MM:SS' 或 'SS' 或 'HH:MM:SS.ms'
# 示例:
# 要去除视频的第 10 秒到第 20 秒:
# TRIM_START = "0:10" 
# TRIM_END = "0:20" 
# 要去除第 1 分钟 30 秒到 2 分钟 05 秒:
TRIM_START = "35:20" 
TRIM_END = "35:40" 
# --- 结束配置 ---


def create_concat_list(file_list, list_path):
    """创建 FFmpeg 拼接所需的文本列表文件"""
    with open(list_path, 'w', encoding='utf-8') as f:
        for fpath in file_list:
            # 使用绝对路径确保 FFmpeg 能够找到
            abs_path = os.path.abspath(fpath)
            f.write(f"file '{abs_path}'\n")
    return list_path


def trim_and_concatenate():
    """执行无损剪切和拼接"""
    print("--- 开始无损剪切任务 ---")

    # 1. 检查输入文件和工具
    if not os.path.exists(VIDEO_INPUT):
        print(f"❌ 错误: 找不到视频文件 '{VIDEO_INPUT}'")
        return
    try:
        subprocess.run(["ffmpeg", "-version"], check=True, capture_output=True)
    except FileNotFoundError:
        print("\n❌ 错误: 找不到 'ffmpeg' 命令。请确保 FFmpeg 已安装并添加到 PATH。")
        return

    # 临时文件路径
    temp_dir = "temp_trim_files"
    os.makedirs(temp_dir, exist_ok=True)
    
    part1_output = os.path.join(temp_dir, "part1.mp4")
    part2_output = os.path.join(temp_dir, "part2.mp4")
    concat_list = os.path.join(temp_dir, "concat_list.txt")

    # --- 第一段剪切: 0 到 TRIM_START ---
    print(f"\n[1/3] 剪切第一段: 从开头到 {TRIM_START}")
    command1 = [
        "ffmpeg",
        "-y",               # 覆盖输出文件
        "-i", VIDEO_INPUT,  # 输入文件
        "-t", TRIM_START,   # 持续时间 (从开头算起)
        "-c", "copy",       # 关键: 无损复制流
        part1_output        # 输出第一段
    ]
    try:
        subprocess.run(command1, check=True, text=True, capture_output=True)
        print("✓ 第一段剪切完成。")
    except subprocess.CalledProcessError as e:
        print(f"❌ 第一段剪切失败。错误: {e.stderr}")
        shutil.rmtree(temp_dir)
        return

    # --- 第二段剪切: TRIM_END 到结尾 ---
    print(f"\n[2/3] 剪切第二段: 从 {TRIM_END} 到结尾")
    command2 = [
        "ffmpeg",
        "-y",               # 覆盖输出文件
        "-ss", TRIM_END,    # 关键: seek 到这个时间点
        "-i", VIDEO_INPUT,  # 输入文件 (注意: -ss 放在 -i 后面，速度更快，但可能略不精确)
        "-c", "copy",       # 关键: 无损复制流
        part2_output        # 输出第二段
    ]
    try:
        subprocess.run(command2, check=True, text=True, capture_output=True)
        print("✓ 第二段剪切完成。")
    except subprocess.CalledProcessError as e:
        print(f"❌ 第二段剪切失败。错误: {e.stderr}")
        shutil.rmtree(temp_dir)
        return

    # 检查两段文件是否都存在且有效
    files_to_concat = []
    if os.path.exists(part1_output) and os.path.getsize(part1_output) > 0:
        files_to_concat.append(part1_output)
    if os.path.exists(part2_output) and os.path.getsize(part2_output) > 0:
        files_to_concat.append(part2_output)

    if not files_to_concat:
        print("❌ 错误: 两段剪切后均无效，无法进行拼接。")
        shutil.rmtree(temp_dir)
        return
    
    # --- 3. 无损拼接 ---
    print(f"\n[3/3] 拼接两段视频: {len(files_to_concat)} 个文件...")
    
    # 3a. 创建拼接列表
    create_concat_list(files_to_concat, concat_list)
    
    # 3b. 执行拼接
    command3 = [
        "ffmpeg",
        "-y",
        "-f", "concat",     # 拼接格式
        "-safe", "0",       # 允许绝对路径
        "-i", concat_list,  # 拼接列表文件
        "-c", "copy",       # 关键: 无损复制流
        VIDEO_OUTPUT
    ]
    
    try:
        subprocess.run(command3, check=True, text=True, capture_output=True)
        
        # 4. 清理
        shutil.rmtree(temp_dir)
        
        print("\n🎉 剪切与拼接成功完成！")
        print(f"输入文件: {VIDEO_INPUT}")
        print(f"剪切区间: 移除 {TRIM_START} 到 {TRIM_END} 的内容")
        print(f"输出文件: {VIDEO_OUTPUT}")

    except subprocess.CalledProcessError as e:
        print(f"❌ 拼接失败。错误: {e.stderr}")
        shutil.rmtree(temp_dir)
    except Exception as e:
        print(f"❌ 发生意外错误: {e}")
        shutil.rmtree(temp_dir)


if __name__ == "__main__":
    trim_and_concatenate()