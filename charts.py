import matplotlib
matplotlib.use('Agg') # Force non-interactive backend for background execution
import matplotlib.pyplot as plt
import seaborn as sns
import os
import json
from typing import List, Dict, Any
import pandas as pd

# Ensure charts directory exists
CHARTS_DIR = os.path.join(os.getcwd(), "public", "charts")
os.makedirs(CHARTS_DIR, exist_ok=True)

def generate_chart(chart_type: str, data: Dict[str, Any], title: str, filename: str) -> str:
    """
    Generates a chart and saves it to public/charts/.
    
    Args:
        chart_type: 'line', 'bar', 'pie'
        data: {
            "labels": ["2020", "2021", ...],
            "datasets": [
                {"label": "Revenue", "data": [10, 20, ...]}
            ]
        }
        title: Chart title
        filename: Output filename (e.g., 'revenue_trend.png')
        
    Returns:
        Absolute path to the saved image.
    """
    try:
        plt.figure(figsize=(10, 6))
        
        # --- 🇨🇳 中文字体修复 ---
        plt.rcParams['font.sans-serif'] = ['SimHei', 'Microsoft YaHei', 'SimSun', 'Arial Unicode MS'] 
        plt.rcParams['axes.unicode_minus'] = False # 解决负号显示问题
        # -----------------------
        
        sns.set_theme(style="whitegrid", font="SimHei") # Seaborn 也需要指定字体
        
        labels = data.get("labels", [])
        datasets = data.get("datasets", [])
        
        if not labels or not datasets:
            return ""

        if chart_type == "line":
            for ds in datasets:
                sns.lineplot(x=labels, y=ds["data"], label=ds.get("label", ""), marker='o')
        elif chart_type == "bar":
            # Multi-dataset bar chart support
            if datasets:
                data_list = []
                for ds in datasets:
                    label = ds.get("label", "Data")
                    values = ds.get("data", [])
                    # Handle case where data length might mismatch labels
                    min_len = min(len(labels), len(values))
                    for i in range(min_len):
                        data_list.append({
                            "Category": labels[i],
                            "Value": values[i],
                            "Series": label
                        })
                
                if data_list:
                    df = pd.DataFrame(data_list)
                    # Use hue to differentiate datasets
                    sns.barplot(data=df, x="Category", y="Value", hue="Series")
                    plt.legend(title="") # Optional: clean up legend title
        
        plt.title(title)
        plt.xticks(rotation=45)
        plt.tight_layout()
        
        filepath = os.path.join(CHARTS_DIR, filename)
        plt.savefig(filepath)
        plt.close()
        
        print(f"  📊 [Charts] Saved chart to {filepath}")
        return filepath
        
    except Exception as e:
        print(f"  ❌ [Charts] Generation failed: {e}")
        return ""

if __name__ == "__main__":
    # Test
    test_data = {
        "labels": ["2021", "2022", "2023", "2024", "2025 (Est)"],
        "datasets": [
            {"label": "NVDA Data Center Revenue ($B)", "data": [10.6, 15.0, 47.5, 95.0, 120.0]}
        ]
    }
    generate_chart("line", test_data, "NVIDIA Data Center Growth", "test_chart.png")
