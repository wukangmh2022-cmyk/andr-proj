import subprocess

def main():
    INPUT_VIDEO = "1.mp4"
    INPUT_AUDIO = "我的音频.mp3"
    OUTPUT_VIDEO = "1_with_new_audio.mp4"

    print(f"开始替换音频...\n输入视频：{INPUT_VIDEO}\n输入音频：{INPUT_AUDIO}")
    
    subprocess.run([
        'ffmpeg',
        '-i', INPUT_VIDEO,          # 输入原视频
        '-i', INPUT_AUDIO,          # 输入新音频
        '-map', '0:v',              # ⭐ 明确映射视频流（从第0个输入，即原视频）
        '-map', '1:a',              # ⭐ 明确映射音频流（从第1个输入，即新音频）
        '-c:v', 'copy',             # ⭐ 视频直接复制（不重新编码，速度快）
        '-c:a', 'aac',              # 音频编码
        '-b:a', '192k',             # 音频比特率
        '-shortest',                # 取较短时长
        '-y',                       # 覆盖输出
        OUTPUT_VIDEO
    ], check=True)

    print(f"\n✅ 音频替换完成！输出文件：{OUTPUT_VIDEO}")

if __name__ == "__main__":
    main()