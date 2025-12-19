from PIL import Image
import os

def precompute_blank_segments(
    img,
    white_threshold=235,      # 判断“白”的亮度阈值，界面偏灰可以调低到 230 左右
    row_ratio_threshold=0.99, # 单行白色像素占比阈值
    sample_step=3,            # 横向采样步长，越大越快但精度略降
    min_segment_height=80     # 空白段最小高度，防止太薄不安全
):
    """
    扫描整张图，找出所有“连续空白行”形成的空白段 [(start_y, end_y), ...]
    """
    gray = img.convert("L")
    width, height = gray.size
    pix = gray.load()

    blank_rows = []
    for y in range(height):
        white = 0
        total = 0
        for x in range(0, width, sample_step):
            total += 1
            if pix[x, y] >= white_threshold:
                white += 1
        ratio = white / total if total else 1.0
        blank_rows.append(ratio >= row_ratio_threshold)

    segments = []
    in_seg = False
    start = 0
    for y, is_blank in enumerate(blank_rows):
        if is_blank and not in_seg:
            in_seg = True
            start = y
        elif not is_blank and in_seg:
            end = y
            if end - start >= min_segment_height:
                segments.append((start, end))
            in_seg = False
    # 收尾
    if in_seg:
        end = height
        if end - start >= min_segment_height:
            segments.append((start, end))

    return segments


def choose_cut_from_segments(segments, target_y, img_height, margin=400):
    """
    在 segments 中选一个离 target_y 最近的空白段中心作为切割位置。
    如果最近的段中心离 target_y 超过 margin，就直接用 target_y。
    """
    if not segments:
        return min(target_y, img_height)

    best_y = None
    best_dist = None

    for s, e in segments:
        center = (s + e) // 2
        dist = abs(center - target_y)
        if best_y is None or dist < best_dist:
            best_y = center
            best_dist = dist

    if best_y is None:
        return min(target_y, img_height)

    if best_dist <= margin:
        return best_y
    else:
        return min(target_y, img_height)


def split_images(
    image_list,
    output_folder="output",
    slice_height=1600,
    min_slice_height=600
):
    """
    image_list: ["1.jpg", "2.png", ...]
    所有输出图片放在同一文件夹下，序号连续
    """
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)

    global_index = 1

    for image_path in image_list:
        if not os.path.exists(image_path):
            print(f"[跳过] 未找到文件：{image_path}")
            continue

        img = Image.open(image_path)
        width, height = img.size
        print(f"▶ 开始切割 {image_path} （{width}×{height}）")

        # 预先算好所有空白段
        segments = precompute_blank_segments(
            img,
            white_threshold=235,
            row_ratio_threshold=0.97,
            sample_step=3,
            min_segment_height=80
        )

        top = 0
        while top < height:
            target_y = top + slice_height

            if target_y >= height:
                bottom = height
            else:
                cut_y = choose_cut_from_segments(
                    segments,
                    target_y,
                    img_height=height,
                    margin=400     # 允许从目标位置上下 400px 内找最近空白带
                )
                bottom = min(cut_y, height)

                # 防止切出太薄一条，如果这一片太小，就直接往下延伸一点
                if bottom - top < min_slice_height and height - top > min_slice_height:
                    bottom = min(top + slice_height, height)

            crop_img = img.crop((0, top, width, bottom))
            save_path = os.path.join(output_folder, f"slice_{global_index}.png")
            crop_img.save(save_path)
            print(f"   - Saved slice_{global_index}.png  (top={top}, bottom={bottom})")

            global_index += 1
            top = bottom

        print(f"✔ 完成：{image_path}\n")

    print("🎉 所有图片切割完成！")


# 使用示例
split_images(
    ["long1.jpg", "long2.jpg", "long3.jpg"],
    slice_height=7288
)
