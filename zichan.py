import pandas as pd
##
# 输入表1作为复制源头，表2作为粘贴源头，
# 输出表3，过程：找到表1的"name"名称的表，
# 如果表头B列为资产编码，然后作为唯一id，在表2的“name2”表的B列匹配到相等id，
# 然后在表2粘贴该行表1的K,L,M,N,O到表2的K,L,M,N,O列，
# 过程打印到控制台，完毕后输出表3，其中2个表名硬编码我自行修改
##
# ================= 配置区（这里按你自己的情况改） =================
# 表1：复制源
SRC_FILE = "表1.xlsx"          # 表1 文件名
SRC_SHEET_NAME = "实验、教学设备"       # 表1 中的工作表名（例如：name）

# 表2：粘贴目标
DEST_FILE = "表2.xlsx"         # 表2 文件名
DEST_SHEET_NAME = "教务科"     # 表2 中的工作表名（例如：name2）

# 输出 表3
OUT_FILE = "表3.xlsx"          # 输出的文件名

# 假设列：A=0, B=1, ..., K=10, L=11, M=12, N=13, O=14
ID_COL_INDEX = 1              # B 列：资产编码，作为唯一 ID
COL_INDEXES_TO_COPY = [11, 12, 13, 14]  # K,L,M,N,O
# =============================================================


def main():
    print(f"读取源文件：{SRC_FILE}，工作表：{SRC_SHEET_NAME}")
    df_src = pd.read_excel(SRC_FILE, sheet_name=SRC_SHEET_NAME, engine="openpyxl")

    print(f"读取目标文件：{DEST_FILE}，工作表：{DEST_SHEET_NAME}")
    df_dest = pd.read_excel(DEST_FILE, sheet_name=DEST_SHEET_NAME, engine="openpyxl")

    # 检查 B 列表头是否为“资产编码”（如果不需要这个检查可以注释掉）
    id_col_name_src = df_src.columns[ID_COL_INDEX]
    print(f"表1 B列表头为：{id_col_name_src}")
    if str(id_col_name_src) != "资产编码":
        print("警告：表1 的 B 列表头不是 '资产编码'，请确认是否正确。")

    # 构建 从 资产编码 -> [K,L,M,N,O...] 的字典，方便后面匹配
    print("构建 表1 的资产编码 -> K~O 列值 的映射...")
    src_map = {}

    for i, row in df_src.iterrows():
        asset_id = row.iloc[ID_COL_INDEX]
        # 跳过空 ID
        if pd.isna(asset_id):
            continue

        values_to_copy = [row.iloc[idx] for idx in COL_INDEXES_TO_COPY]
        src_map[asset_id] = values_to_copy

    print(f"表1 中共找到 {len(src_map)} 个有资产编码的记录。")

    # 开始在 表2 中扫描匹配，并写入 K~O 列
    print("开始在 表2 中匹配资产编码，并写入 K~O 列...")
    match_count = 0
    no_match_count = 0

    for i, row in df_dest.iterrows():
        dest_asset_id = row.iloc[ID_COL_INDEX]

        # 如果目标行 B 列为空，跳过
        if pd.isna(dest_asset_id):
            print(f"第 {i+2} 行：目标 B 列为空，跳过。")
            continue

        if dest_asset_id in src_map:
            values_from_src = src_map[dest_asset_id]
            # 打印匹配信息
            print(f"匹配成功：资产编码 {dest_asset_id} （表2 第 {i+2} 行）")
            print(f"    写入 K~O 列值：{values_from_src}")

            # 写入 df_dest 对应的 K~O 列
            for col_pos, val in zip(COL_INDEXES_TO_COPY, values_from_src):
                df_dest.iat[i, col_pos] = val

            match_count += 1
        else:
            print(f"未匹配：资产编码 {dest_asset_id} （表2 第 {i+2} 行）")
            no_match_count += 1

    print("匹配完成。")
    print(f"匹配成功行数：{match_count}")
    print(f"未匹配行数：{no_match_count}")

    # 将更新后的 df_dest 输出为 表3
    print(f"写出结果到：{OUT_FILE}")
    # 如果你只需要导出这一张工作表，可以这样：
    with pd.ExcelWriter(OUT_FILE, engine="openpyxl") as writer:
        df_dest.to_excel(writer, sheet_name=DEST_SHEET_NAME, index=False)

    print("全部完成！")


if __name__ == "__main__":
    main()
