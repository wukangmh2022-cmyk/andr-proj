import numpy as np
import matplotlib.pyplot as plt
from datetime import datetime, date
from lunardate import LunarDate
import pandas as pd
from collections import defaultdict

def find_reunion_age(birth_date, max_age=100):
    """
    计算一个人的农历生日与公历出生日期重逢的年龄
    """
    birth_year = birth_date.year
    lunar_birth = LunarDate.fromSolarDate(birth_year, birth_date.month, birth_date.day)
    reunion_ages = []
    
    for age in range(1, max_age + 1):
        check_year = birth_year + age
        try:
            # 计算农历生日对应的公历日期
            solar_date = LunarDate(check_year, lunar_birth.month, lunar_birth.day).toSolarDate()
            
            # 检查是否与出生日期匹配
            if solar_date.month == birth_date.month and solar_date.day == birth_date.day:
                reunion_ages.append(age)
        except:
            # 处理农历日期转换可能的异常
            continue
            
    return reunion_ages

def analyze_decade_reunion(start_year, end_year, samples_per_year=10):
    """
    分析一个年代出生的人的重逢年龄分布
    """
    reunion_ages = []
    
    for year in range(start_year, end_year + 1):
        for month in range(1, 13):
            # 每月取几个样本日
            for day_sample in range(1, samples_per_year + 1):
                day = max(1, min(28, day_sample * 3))  # 简单生成一些日期
                try:
                    birth_date = date(year, month, day)
                    ages = find_reunion_age(birth_date)
                    if ages:
                        reunion_ages.extend(ages)
                except:
                    continue
    
    return reunion_ages

# 设置中文字体
plt.rcParams['font.sans-serif'] = ['SimHei']
plt.rcParams['axes.unicode_minus'] = False

# 分析不同年代
decades = {
    "60s": (1960, 1969),
    "70s": (1970, 1979),
    "80s": (1980, 1989),
    "90s": (1990, 1999)
}

results = {}

print("开始计算各年代出生者的农历与公历生日重逢年龄...")
for decade, (start, end) in decades.items():
    print(f"分析{decade}年代...")
    ages = analyze_decade_reunion(start, end, samples_per_year=5)
    if ages:
        avg_age = np.mean(ages)
        results[decade] = {
            "平均重逢年龄": avg_age,
            "样本数": len(ages),
            "年龄分布": ages
        }
        print(f"{decade}年代: 平均{avg_age:.1f}岁重逢 (样本数: {len(ages)})")
    else:
        print(f"{decade}年代: 未找到重逢数据")

# 可视化结果
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 6))

# 柱状图：各年代平均重逢年龄
decades_list = list(results.keys())
avg_ages = [results[d]['平均重逢年龄'] for d in decades_list]
sample_counts = [results[d]['样本数'] for d in decades_list]

bars = ax1.bar(decades_list, avg_ages, color=['#FF9999', '#66B2FF', '#99FF99', '#FFCC99'])
ax1.set_title('各年代出生者农历与公历生日重逢平均年龄')
ax1.set_ylabel('年龄(岁)')
ax1.set_xlabel('出生年代')

# 在柱子上添加数值标签
for bar, value in zip(bars, avg_ages):
    height = bar.get_height()
    ax1.text(bar.get_x() + bar.get_width()/2., height + 0.5,
             f'{value:.1f}', ha='center', va='bottom')

# 箱线图：各年代重逢年龄分布
all_ages = [results[d]['年龄分布'] for d in decades_list]
ax2.boxplot(all_ages, labels=decades_list)
ax2.set_title('各年代重逢年龄分布')
ax2.set_ylabel('年龄(岁)')
ax2.set_xlabel('出生年代')

plt.tight_layout()
plt.show()

# 显示详细统计数据
print("\n详细统计数据:")
for decade, data in results.items():
    ages = data['年龄分布']
    print(f"\n{decade}年代:")
    print(f"  平均年龄: {np.mean(ages):.2f}岁")
    print(f"  中位数: {np.median(ages):.2f}岁")
    print(f"  最小年龄: {np.min(ages)}岁")
    print(f"  最大年龄: {np.max(ages)}岁")
    print(f"  标准差: {np.std(ages):.2f}")
    print(f"  样本数: {len(ages)}")

# 显示一些具体的重逢周期例子
print("\n\n特定日期重逢例子:")
example_dates = [
    (1997, 9, 9),  # 已知29年重逢的例子
    (1985, 3, 15), # 随机选择
    (1975, 6, 20), # 随机选择
    (1965, 11, 8)  # 随机选择
]

for y, m, d in example_dates:
    try:
        birth_date = date(y, m, d)
        lunar_birth = LunarDate.fromSolarDate(y, m, d)
        reunion_ages = find_reunion_age(birth_date)
        
        print(f"\n出生日期: {y}-{m}-{d} (农历: {lunar_birth.month}/{lunar_birth.day})")
        if reunion_ages:
            for age in reunion_ages:
                reunion_year = y + age
                print(f"  将在 {age} 岁时的 {reunion_year} 年重逢")
        else:
            print(f"  在100年内未发现重逢")
    except:
        print(f"出生日期 {y}-{m}-{d} 计算错误")
