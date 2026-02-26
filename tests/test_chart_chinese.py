
import matplotlib.pyplot as plt
import seaborn as sns
import os

def test_chinese_font():
    try:
        # Configuration (same as in charts.py)
        plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'SimSun', 'Arial Unicode MS'] 
        plt.rcParams['axes.unicode_minus'] = False
        sns.set_theme(style="whitegrid", font="SimHei")

        # Data
        x = ["一月", "二月", "三月"]
        y = [10, 20, 15]

        plt.figure(figsize=(6, 4))
        sns.barplot(x=x, y=y)
        plt.title("测试中文标题 - Test Chinese Title")
        plt.xlabel("月份")
        plt.ylabel("数值")

        output_path = "test_chinese_chart.png"
        plt.savefig(output_path)
        print(f"✅ Generated {output_path}")
        
    except Exception as e:
        print(f"❌ Failed: {e}")

if __name__ == "__main__":
    test_chinese_font()
