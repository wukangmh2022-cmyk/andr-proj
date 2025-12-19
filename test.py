import asyncio
import edge_tts
import os
import re
import subprocess
import shutil
import math
from datetime import datetime

# 1. 定义输出文件名
OUTPUT_FILE = "break_test_final.mp3"

# 2. 定义声音 (Voice)
VOICE = "en-US-JennyNeural"

# 3. **最终修正的 SSML 文本**
#    - 确保了正确的 xmlns
#    - 确保所有内容都被 <voice> 标签包裹
SSML_TEXT = f"""
<speak version="1.0" xmlns="http://www.w3.org/2001/10/synthesis" xml:lang="en-US">
    <voice name="{VOICE}">
        Hello, this is a test of the break tag. 
        <break time="5s"/> 
        Did you hear the five second pause?
    </voice>
</speak>
"""

async def generate_audio_with_break(ssml_text: str, voice: str, output_file: str):
    """
    使用 edge_tts 库根据 SSML 文本生成音频文件。
    注意：在 Communicator 实例化时，仍需传入 voice 参数。
    """
    print(f"开始生成音频文件: {output_file}")
    print(f"使用的声音 (传入): {voice}")
    print(f"SSML内容:\n{ssml_text.strip()}")

    try:
        # 当传入完整的 SSML 文本时，edge_tts 应该自动识别并使用 SSML 内部的设置。
        # 但为了兼容性和鲁棒性，我们继续传入 voice 参数。
        communicator = edge_tts.Communicate(ssml_text, voice)
        
        # 将音频流保存到文件
        await communicator.save(output_file)
        
        print(f"\n✅ 音频文件已成功保存到: {os.path.abspath(output_file)}")
        print("请播放此文件以确认 5 秒停顿效果。")

    except Exception as e:
        print(f"\n❌ 生成音频时发生错误: {e}")
        # 尝试打印 SSML 长度，有时过长的 SSML 会被忽略（搜索结果 1.8, 1.9 提到）
        print(f"SSML 长度: {len(ssml_text)}")


# 4. 运行主函数
if __name__ == "__main__":
    # 使用 asyncio.run 运行异步生成函数
    asyncio.run(generate_audio_with_break(SSML_TEXT, VOICE, OUTPUT_FILE))