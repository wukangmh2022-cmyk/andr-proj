import re
import asyncio
import edge_tts
import os
import subprocess
import shutil
from datetime import datetime
from typing import List, Tuple, Optional

class SrtTTSGenerator:
    def __init__(self, max_concurrent_tasks=16):
        self.temp_dir = os.path.join(os.getcwd(), 'temp')
        os.makedirs(self.temp_dir, exist_ok=True)
        self.semaphore = asyncio.Semaphore(max_concurrent_tasks)
        
        # [新增] 时长偏差配置 (用于串行日志)
        self.max_duration_deviation_ratio = 0.02  # 2%
        self.min_duration_deviation_sec = 0.050 # 50ms (绝对阈值)
        # [新增] 累计偏差阈值
        self.max_cumulative_deviation_sec = 1.0 # 允许的最大累计偏差 (秒)

        print(f"临时文件目录: {self.temp_dir}")
        print(f"最大并发任务数: {max_concurrent_tasks}")
        print(f"✓ 策略: [高精度 WAV 模式] - TTS 较短则填充静音，TTS 较长则加速处理。")

    def read_text_file(self, file_path):
        """从文件读取文本内容"""
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                return f.read()
        except FileNotFoundError:
            print(f"❌ 错误: 文件 {file_path} 不存在")
            return None
        except Exception as e:
            print(f"❌ 读取文件时出错: {e}")
            return None

    def time_to_seconds_srt(self, time_str):
        """将 SRT 时间字符串 (HH:MM:SS,mmm) 转换为秒数"""
        try:
            parts = time_str.split(':')
            h = int(parts[0])
            m = int(parts[1])
            sec_ms = parts[2].split(',')
            s = int(sec_ms[0])
            ms = int(sec_ms[1])
            total_sec = (h * 3600) + (m * 60) + s + (ms / 1000.0)
            return total_sec
        except Exception as e:
            print(f"❌ 错误的时间格式: {time_str} - {e}")
            return 0

    def parse_srt_file(self, srt_content):
        """解析 SRT 文件内容,返回: [(start_sec, end_sec, text), ...]"""
        segments = []
        pattern = re.compile(
            r'(\d+)\n'
            r'(\d{2}:\d{2}:\d{2},\d{3}) --> (\d{2}:\d{2}:\d{2},\d{3})\n'
            r'([\s\S]*?)(?=\n\n|\Z)',
            re.MULTILINE
        )
        for match in pattern.finditer(srt_content):
            try:
                start_time_str = match.group(2)
                end_time_str = match.group(3)
                text_block = match.group(4)
                start_sec = self.time_to_seconds_srt(start_time_str)
                end_sec = self.time_to_seconds_srt(end_time_str)
                text = re.sub(r'\s+', ' ', text_block).strip()
                if text:
                    segments.append((start_sec, end_sec, text))
            except Exception as e:
                print(f"❌ 解析SRT条目失败: {match.group(0)} - {e}")
        print(f"✓ SRT 解析完成，共 {len(segments)} 个片段")
        return segments

    async def generate_tts_with_retry(self, text, output_file_wav, voice, rate, max_retries=3):
        """[修改] 生成 TTS 并立即转为 WAV"""
        for attempt in range(max_retries):
            try:
                async with self.semaphore:
                    # Edge-TTS 只能生成 MP3
                    temp_mp3 = output_file_wav + ".temp_tts.mp3"
                    communicate = edge_tts.Communicate(text, voice, rate=rate)
                    await communicate.save(temp_mp3)
                    
                    if not os.path.exists(temp_mp3):
                        raise Exception("TTS生成MP3文件不存在")
                    
                    # [关键] 立即转换为 WAV (PCM 16-bit)
                    subprocess.run([
                        'ffmpeg', '-i', temp_mp3,
                        '-ar', '44100', '-ac', '2', '-f', 'wav', '-c:a', 'pcm_s16le',
                        '-y', output_file_wav
                    ], capture_output=True, text=True, check=True)
                    
                    actual_duration = self.get_audio_duration(output_file_wav)
                    os.remove(temp_mp3)
                    return output_file_wav, actual_duration
            except Exception as e:
                if attempt < max_retries - 1:
                    await asyncio.sleep(1 * (attempt + 1))  # 递增延迟
                    continue
                print(f" ✗ TTS生成失败 (重试{max_retries}次): {e}")
                for f in [temp_mp3, output_file_wav]:
                    if os.path.exists(f): 
                        os.remove(f)
                return None, 0

    async def create_silence(self, output_file_wav, duration):
        """[修改] 生成静音 (异步 WAV)"""
        try:
            await asyncio.to_thread(
                self.create_silence_sync, output_file_wav, duration
            )
            return output_file_wav if os.path.exists(output_file_wav) else None
        except Exception as e:
            print(f" ✗ 生成静音失败: {e}")
            return None

    def create_silence_sync(self, output_file_wav, duration):
        """[修改] 生成静音 (同步 WAV)"""
        try:
            if duration <= 0:
                return None
            subprocess.run([
                'ffmpeg',
                '-f', 'lavfi', '-t', str(duration), '-i', 'anullsrc=r=44100:cl=stereo',
                '-ar', '44100', '-ac', '2', '-f', 'wav', '-c:a', 'pcm_s16le',
                '-y', output_file_wav
            ], capture_output=True, text=True, check=True)
            return output_file_wav
        except Exception as e:
            print(f" ✗ [Sync] 生成静音WAV失败: {e}")
            return None


    def get_audio_duration(self, audio_file):
        """获取音频文件时长 (WAV 非常精确)"""
        try:
            if not os.path.exists(audio_file) or os.path.getsize(audio_file) == 0:
                return 0
            result = subprocess.run([
                'ffprobe', '-v', 'error',
                '-show_entries', 'format=duration',
                '-of', 'default=noprint_wrappers=1:nokey=1',
                audio_file
            ], capture_output=True, text=True)
            return float(result.stdout.strip())
        except:
            return 0

    def stretch_audio_to_duration(self, input_file_wav, output_file_wav, target_duration):
        """
        [修改] WAV 高精度处理
        - 如果 TTS 时长 < 目标时长: 保持原速，尾部填充静音。
        - 如果 TTS 时长 > 目标时长: 加速（压缩）音频以匹配目标。
        """
        try:
            current_duration = self.get_audio_duration(input_file_wav)
            
            # Case 0: 时长几乎一致 (容忍 5ms 误差, WAV可以更精确)
            if abs(current_duration - target_duration) < 0.005:
                shutil.copy(input_file_wav, output_file_wav)
                return True, "no_change"

            # Case 1: TTS 时长 < 目标时长 (保持原速，尾部填充静音)
            if current_duration < target_duration:
                padding_duration = target_duration - current_duration
                
                silence_file = output_file_wav + ".temp_silence.wav"
                if not self.create_silence_sync(silence_file, padding_duration):
                    raise Exception("Failed to create silence for padding")

                # 拼接 [input_file] + [silence_file] -> [output_file]
                list_file = output_file_wav + '.concat_list.txt'
                with open(list_file, 'w', encoding='utf-8') as f:
                    f.write(f"file '{os.path.abspath(input_file_wav)}'\n")
                    f.write(f"file '{os.path.abspath(silence_file)}'\n")
                
                # [关键] WAV 使用 -c copy 快速无损拼接
                subprocess.run([
                    'ffmpeg', '-f', 'concat', '-safe', '0', '-i', list_file,
                    '-c', 'copy', # 复制 WAV 数据流，极快且无损
                    '-y', output_file_wav
                ], capture_output=True, text=True, check=True)
                
                os.remove(silence_file)
                os.remove(list_file)
                return True, "原速+静音填充"

            # Case 2: TTS 时长 > 目标时长 (加速音频)
            else: # current_duration > target_duration
                atempo_factor = current_duration / target_duration # 因子 > 1.0

                filter_chain = []
                while atempo_factor > 2.0:
                    filter_chain.append("atempo=2.0")
                    atempo_factor /= 2.0
                
                safe_factor = max(0.5, min(2.0, atempo_factor))
                filter_chain.append(f"atempo={safe_factor:.3f}")

                filter_string = ",".join(filter_chain)
                
                temp_stretched = output_file_wav + ".temp_stretched.wav"
                subprocess.run([
                    'ffmpeg', '-i', input_file_wav,
                    '-filter:a', filter_string,
                    '-f', 'wav', '-c:a', 'pcm_s16le',
                    '-y', temp_stretched
                ], capture_output=True, text=True, check=True)
                
                # [关键] 修正 atempo 的微小误差 (截断或补齐)
                stretched_duration = self.get_audio_duration(temp_stretched)
                residual = target_duration - stretched_duration
                
                if abs(residual) < 0.005: # 5ms 内，可接受
                    shutil.move(temp_stretched, output_file_wav)
                elif residual > 0: # 加速后文件略短，补静音
                    self.stretch_audio_to_duration(temp_stretched, output_file_wav, target_duration)
                    os.remove(temp_stretched)
                elif residual < 0: # 加速后文件略长，截断
                    subprocess.run([
                        'ffmpeg', '-i', temp_stretched,
                        '-t', str(target_duration), # 截断到目标时长
                        '-c', 'copy',
                        '-y', output_file_wav
                    ], capture_output=True, text=True, check=True)
                    os.remove(temp_stretched)

                return True, "加速"

        except Exception as e:
            print(f" ✗ 音频处理 (加速/填充) 失败: {e}")
            shutil.copy(input_file_wav, output_file_wav)
            return False, "error"

    def concatenate_audio_files(self, audio_files, output_wav):
        """[修改] 拼接 WAV 文件 (使用 -c copy)"""
        valid_files = [f for f in audio_files if os.path.exists(f) and os.path.getsize(f) > 0]
        if not valid_files:
            print("❌ 没有有效的 WAV 文件")
            return False
        
        print(f"\n拼接 {len(valid_files)} 个 WAV 片段...")
        list_file = os.path.join(self.temp_dir, 'concat_list.txt')
        
        with open(list_file, 'w', encoding='utf-8') as f:
            for audio_file in valid_files:
                abs_path = os.path.abspath(audio_file)
                f.write(f"file '{abs_path}'\n")
        
        try:
            subprocess.run([
                'ffmpeg',
                '-f', 'concat', '-safe', '0', '-i', list_file,
                '-c', 'copy', # WAV 必须用 copy
                '-y', output_wav
            ], capture_output=True, text=True, check=True)
            
            if os.path.exists(list_file): 
                os.remove(list_file)
            
            if os.path.exists(output_wav) and os.path.getsize(output_wav) > 0:
                final_duration = self.get_audio_duration(output_wav)
                print(f"✓ WAV 拼接成功 (总时长: {final_duration:.1f}秒)")
                return True
            else:
                print("❌ 拼接后 WAV 文件无效")
                return False
        except Exception as e:
            print(f"❌ WAV 拼接失败: {e}")
            if os.path.exists(list_file): 
                os.remove(list_file)
            return False

    def convert_wav_to_mp3(self, wav_file, mp3_file):
        """[新增] 最终将 WAV 转换为 MP3"""
        print(f"\n正在将 {wav_file} 转换为 {mp3_file}...")
        try:
            subprocess.run([
                'ffmpeg', '-i', wav_file,
                '-c:a', 'libmp3lame', '-b:a', '192k',
                '-y', mp3_file
            ], capture_output=True, text=True, check=True)
            
            if os.path.exists(mp3_file):
                print("✓ MP3 转换成功")
                return True
            return False
        except Exception as e:
            print(f"❌ WAV 转 MP3 失败: {e}")
            return False


    async def process_single_segment(self, i, start_sec, end_sec, text, voice, rate, current_time_sec):
        """[修改] 处理单个片段, 输出 WAV, 并返回文件列表"""
        
        segment_files_tuples = [] # 存储 ('gap'/'audio', file_path)
        
        # 1. 生成静音间隙
        gap_duration_target = start_sec - current_time_sec
        
        if gap_duration_target > 0.05:
            silence_file = os.path.join(self.temp_dir, f"seg_{i:04d}_gap.wav") # .wav
            res = await self.create_silence(silence_file, gap_duration_target)
            if res:
                segment_files_tuples.append(('gap', silence_file))
            else:
                print(f"✗ 警告: 片段 {i:04d} 静音生成失败。")
        
        # 2. 生成TTS音频
        target_tts_duration = end_sec - start_sec
        if target_tts_duration <= 0.01:
             # 这是一个纯静音片段
            return segment_files_tuples, True

        tts_raw_file = os.path.join(self.temp_dir, f"seg_{i:04d}_raw.wav") # .wav
        tts_final_file = os.path.join(self.temp_dir, f"seg_{i:04d}_final.wav") # .wav
        
        result, actual_duration = await self.generate_tts_with_retry(text, tts_raw_file, voice, rate)
        if not result:
            return segment_files_tuples, False # TTS 彻底失败
        
        # 3. 拉伸(加速)/填充静音
        success, action = await asyncio.to_thread(
            self.stretch_audio_to_duration,
            tts_raw_file, tts_final_file, target_tts_duration
        )
        
        if not success:
            print(f" ✗ 片段 {i:04d} 处理失败 (Action: {action})，使用了回退文件。")
            
        if os.path.exists(tts_raw_file):
            os.remove(tts_raw_file)
        
        segment_files_tuples.append(('audio', tts_final_file))
        return segment_files_tuples, True
    
    
    def validate_segment_durations(self, segments, results):
        """
        [新增] 串行时长校验 (在拼接前运行)
        读取所有生成的 WAV 文件，计算累计偏差
        """
        print(f"\n{'='*60}")
        print(f"🔬 开始执行拼接前串行时长校验...")
        
        cumulative_deviation = 0.0
        last_srt_end_sec = 0.0
        
        for i, (start_sec, end_sec, text) in enumerate(segments):
            
            result = results[i]
            if isinstance(result, Exception):
                print(f"[{i:04d}] ✗ 跳过校验 (任务执行失败)")
                continue

            segment_files, success = result
            if not success:
                print(f"[{i:04d}] ✗ 跳过校验 (片段处理失败)")
                continue

            actual_gap_duration = 0.0
            actual_tts_duration = 0.0
            
            # 1. 直接读取文件获取实际时长
            for file_type, file_path in segment_files:
                duration = self.get_audio_duration(file_path)
                if file_type == 'gap':
                    actual_gap_duration = duration
                elif file_type == 'audio':
                    actual_tts_duration = duration

            actual_total_duration = actual_gap_duration + actual_tts_duration
            
            # 2. 计算目标时长
            target_total_duration = end_sec - last_srt_end_sec
            target_gap_duration = start_sec - last_srt_end_sec
            target_tts_duration = end_sec - start_sec

            # 3. 计算偏差
            deviation = actual_total_duration - target_total_duration
            cumulative_deviation += deviation
            
            allowed_deviation_abs = max(
                target_total_duration * self.max_duration_deviation_ratio, 
                self.min_duration_deviation_sec
            )
            
            # 4. 打印警告
            if abs(deviation) > allowed_deviation_abs:
                print(f"⚠️  [时长警告] 片段 {i:04d}: 目标 {target_total_duration:.3f}s, 实际 {actual_total_duration:.3f}s. "
                      f"偏差 {deviation:+.3f}s (累计 {cumulative_deviation:+.3f}s)")
                # (可选) 详细日志
                # print(f"    ... 目标: (Gap: {target_gap_duration:.3f}s + TTS: {target_tts_duration:.3f}s)")
                # print(f"    ... 实际: (Gap: {actual_gap_duration:.3f}s + TTS: {actual_tts_duration:.3f}s) - {text[:30]}...")

            last_srt_end_sec = end_sec

        # 最终总结
        print(f"\n--- 校验完毕 ---")
        print(f"SRT 目标总时长: {last_srt_end_sec:.3f}s")
        print(f"WAV 累计总时长: {(last_srt_end_sec + cumulative_deviation):.3f}s")
        print(f"最终累计偏差: {cumulative_deviation:+.3f}s")
        
        if abs(cumulative_deviation) > self.max_cumulative_deviation_sec:
            print(f"❌ 错误: 累计偏差 ({cumulative_deviation:.3f}s) 超过阈值 ({self.max_cumulative_deviation_sec:.3f}s)。")
            print(f"❌ 终止拼接以避免音画不同步。请检查上方 [时长警告] 日志。")
            return False
        
        print(f"✓ 累计偏差在允许范围内。")
        return True


    async def generate_audio_from_file(self, input_file="1.srt", output_file="我的音频.mp3", 
                                      voice='zh-CN-XiaoyiNeural', rate='+0%'):
        """[修改] 完整流程: WAV -> 校验 -> 拼接 -> MP3"""
        start_time = datetime.now()
        print(f"\n{'='*60}")
        print(f"开始处理 SRT: {input_file}")
        print(f"语速设置: {rate}")
        print(f"并发线程: {self.semaphore._value}")
        print(f"{'='*60}\n")
        
        srt_content = self.read_text_file(input_file)
        if srt_content is None: return None
        segments = self.parse_srt_file(srt_content)
        if not segments: return None
        
        total_segments = len(segments)
        print(f"\n=== (1/4) 开始并发处理 {total_segments} 个 WAV 片段 ===\n")
        
        tasks = []
        current_time_sec = 0.0
        for i, (start_sec, end_sec, text) in enumerate(segments):
            task = self.process_single_segment(i, start_sec, end_sec, text, voice, rate, current_time_sec)
            tasks.append(task)
            current_time_sec = end_sec
        
        # 1. 并发执行
        print(f"🚀 启动 {len(tasks)} 个并发任务...")
        results = await asyncio.gather(*tasks, return_exceptions=True)
        print(f"\n✓ 并发处理完成。")
        
        # 2. [新增] 串行校验
        if not self.validate_segment_durations(segments, results):
            return None # 校验失败，终止
        
        # 3. 收集文件并拼接
        print(f"\n=== (3/4) 开始拼接 WAV 文件 ===\n")
        audio_files_to_concat = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                print(f"[{i+1}/{total_segments}] ✗ 跳过拼接 (任务失败: {result})")
                continue
            
            segment_files, success = result
            if not success:
                print(f"[{i+1}/{total_segments}] ✗ 跳过拼接 (片段处理失败)")
                continue

            for file_type, file_path in segment_files:
                audio_files_to_concat.append(file_path)
        
        # 拼接为最终 WAV
        final_wav = os.path.join(self.temp_dir, "final_output.wav")
        if not self.concatenate_audio_files(audio_files_to_concat, final_wav):
            print("❌ WAV 拼接失败")
            return None
            
        # 4. 转换
        print(f"\n=== (4/4) 转换为 MP3 ===\n")
        if self.convert_wav_to_mp3(final_wav, output_file):
            end_time = datetime.now()
            processing_time = (end_time - start_time).total_seconds()
            
            if os.path.exists(output_file):
                final_size = os.path.getsize(output_file) / 1024 / 1024
                final_duration = self.get_audio_duration(output_file)
                print(f"\n🎉 处理完成！")
                print(f"📁 输出文件: {output_file}")
                print(f"📊 文件大小: {final_size:.2f} MB")
                print(f"⏱️ 音频时长: {final_duration:.1f} 秒 ({final_duration/60:.1f} 分钟)")
                print(f"⏰ 处理时间: {processing_time:.1f} 秒")
                print(f"🚀 平均速度: {total_segments/processing_time:.1f} 片段/秒")
                print(f"💡 提示: 临时文件 (WAV) 保存在 {self.temp_dir}")
                return output_file
        
        print("❌ 最终 MP3 转换失败")
        return None


async def main():
    generator = SrtTTSGenerator(max_concurrent_tasks=16)
    
    output_file = await generator.generate_audio_from_file(
        input_file="1_原文.srt",
        output_file="我的音频_from_srt.mp3",
        voice='zh-CN-XiaoyiNeural',
        rate='+0%'
    )
    
    if output_file:
        print("\n🎉 处理完成！")
    else:
        print("\n💔 处理失败，请检查上述错误信息")


if __name__ == "__main__":
    if os.name == 'nt':
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())