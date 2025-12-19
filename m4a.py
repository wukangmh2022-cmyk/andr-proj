import os
import subprocess

def m4a_to_mp3():
    # 硬编码文件路径
    input_file = "1.m4a"  # 输入M4A文件
    output_file = "1.mp3"  # 输出MP3文件
    
    # 检查文件是否存在
    if not os.path.exists(input_file):
        print(f"错误: 输入文件 {input_file} 不存在")
        print("请将M4A文件重命名为 'input.m4a' 并放在当前目录")
        return
    
    print(f"开始转换: {input_file} → {output_file}")
    
    # 执行ffmpeg命令
    cmd = [
        'ffmpeg',
        '-i', input_file,      # 输入文件
        '-codec:a', 'libmp3lame',  # 使用MP3编码器
        '-q:a', '2',           # 音质设置 (0-9, 0最好)
        '-y',                  # 覆盖输出文件
        output_file
    ]
    
    try:
        # 运行ffmpeg
        result = subprocess.run(cmd, capture_output=True, text=True)
        
        if result.returncode == 0:
            print(f"✓ 转换完成！输出文件: {output_file}")
            
            # 显示文件大小信息
            if os.path.exists(output_file):
                size = os.path.getsize(output_file) / (1024 * 1024)
                print(f"文件大小: {size:.2f} MB")
        else:
            print("✗ 转换失败")
            if result.stderr:
                print("错误信息:", result.stderr[:200])
            
    except FileNotFoundError:
        print("错误: 未找到ffmpeg，请先安装ffmpeg")
    except Exception as e:
        print(f"错误: {e}")

def batch_convert_m4a_to_mp3():
    """批量转换当前目录下所有M4A文件"""
    print("开始批量转换M4A文件...")
    
    converted_count = 0
    
    for file in os.listdir('.'):
        if file.lower().endswith('.m4a'):
            input_file = file
            output_file = os.path.splitext(file)[0] + '.mp3'
            
            print(f"转换: {input_file} → {output_file}")
            
            cmd = [
                'ffmpeg', '-i', input_file,
                '-codec:a', 'libmp3lame', '-q:a', '2',
                '-y', output_file
            ]
            
            try:
                result = subprocess.run(cmd, capture_output=True)
                if result.returncode == 0:
                    print(f"  ✓ 完成: {output_file}")
                    converted_count += 1
                else:
                    print(f"  ✗ 失败: {input_file}")
                    
            except Exception:
                print(f"  ✗ 错误: {input_file}")
    
    print(f"\n批量转换完成: {converted_count} 个文件")

if __name__ == "__main__":
    # 单个文件转换
    m4a_to_mp3()
    
    # 如果想要批量转换，取消下面的注释
    # batch_convert_m4a_to_mp3()
