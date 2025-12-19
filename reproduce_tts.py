import asyncio
import edge_tts
import traceback
import tempfile
import os

async def main():
    text = "测试语音播报"
    voice = "zh-CN-YunxiNeural"
    print(f"Testing edge-tts with voice={voice}...")
    try:
        comm = edge_tts.Communicate(text, voice)
        
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
            out = f.name
        
        print(f"Saving to {out}...")
        await comm.save(out)
        print(f"Success! File size: {os.path.getsize(out)}")
        os.unlink(out)
    except Exception:
        print("Caught exception:")
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
