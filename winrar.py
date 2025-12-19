import os
import zipfile

def zip_with_gbk_encoding(src_dir, zip_name="output.zip"):
    with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(src_dir):
            for file in files:
                filepath = os.path.join(root, file)

                # 去掉根路径，得到相对路径
                arcname = os.path.relpath(filepath, src_dir)

                # 关键：把 UTF-8 文件名转换成 GBK 字节
                gbk_name = arcname.encode("gbk", errors="ignore")

                # 写入 ZIP（用 GBK 文件名）
                z.writestr(gbk_name, open(filepath, "rb").read())

    print(f"已生成 GBK 编码 ZIP：{zip_name}")

# 使用示例
zip_with_gbk_encoding("/Users/pippo/Documents/AI护理菁英班_收集相 关单位_打包全集 ", "windows_compatible.zip")
