from PIL import Image
import os

def process_image(input_path, output_path):
    """处理单张图片：G通道>0则设为0，不透明度=255-原G值"""
    try:
        with Image.open(input_path).convert("RGBA") as img:
            width, height = img.size
            pixels = img.load()
            
            for x in range(width):
                for y in range(height):
                    r, g, b, a = pixels[x, y]
                    if g > 0:
                        new_a = max(0, min(255, 255 - g))  # 确保Alpha在0-255范围
                        pixels[x, y] = (r, 0, b, new_a)
            
            # 保存处理后的图片（保留原文件名，转换为PNG格式）
            img.save(output_path, "PNG")
            print(f"处理完成：{os.path.basename(input_path)} -> {os.path.basename(output_path)}")
    except Exception as e:
        print(f"处理失败 {input_path}：{str(e)}")

def batch_process_images(input_dir="/img", output_dir="/img_out"):
    """批量处理目录下所有图片"""
    # 创建输出目录（如果不存在）
    os.makedirs(output_dir, exist_ok=True)
    
    # 支持的图片格式（可根据需要扩展）
    supported_formats = ('.png', '.jpg', '.jpeg', '.bmp', '.gif', '.tiff')
    
    # 遍历输入目录所有文件
    for filename in os.listdir(input_dir):
        # 跳过非图片文件
        if not filename.lower().endswith(supported_formats):
            continue
        
        input_path = os.path.join(input_dir, filename)
        # 输出文件名：保留原文件名，替换扩展名为.png
        output_filename = os.path.splitext(filename)[0] + ".png"
        output_path = os.path.join(output_dir, output_filename)
        
        # 处理图片
        process_image(input_path, output_path)

if __name__ == "__main__":
    # 批量处理 /img 目录，输出到 /img_out
    batch_process_images(input_dir="img", output_dir="img_out")
    print("所有图片处理完成！结果保存在 /img_out 目录")
