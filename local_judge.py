import json
import re
import os
import time
from openai import OpenAI

from config import SILICONFLOW_API_KEY, MODEL_FAST

# 初始化裁判模型 (使用本地 Llama-3/Ollama 降低成本)
client = OpenAI(
    api_key="ollama", # placeholder needed for the client
    base_url="http://localhost:11434/v1"
)

INPUT_FILE = "data/sft_training_data.jsonl"
OUTPUT_SFT = "data/curated_sft_data.jsonl"  # 高分通过的训练集
OUTPUT_DPO = "data/rejected_dpo_data.jsonl" # 低分淘汰的负样本集

def get_citation_score(report_text):
    """硬指标：计算引用密度 (每千字几个链接)"""
    urls = re.findall(r'https?://[^\s<>"]+|www\.[^\s<>"]+', report_text)
    word_count = len(report_text)
    if word_count == 0: return 0, 0
    
    # 假设博士级调研每千字至少需要 3 个真实引用
    density = (len(urls) / word_count) * 1000 
    
    # 密度转换为 0-10 的基础分
    base_score = min(10.0, density * 2) 
    return base_score, len(urls)

def get_llm_structure_score(report_text):
    """软指标：让 LLM 评价结构和深度 (返回 0-10 分)"""
    # 绝大部分现代模型上下文窗口都足够大，直接传入全文即可，防止截断导致误判
    context = report_text

    prompt = f"""You are an expert academic reviewer. Evaluate this research report on a scale of 1 to 10.
    Focus on:
    1. Structure (Does it have an Abstract, Body, Conclusion?)
    2. Depth of insight (Is it analytical or just superficial?)
    
    Report:
    {context}
    
    Output strictly in this JSON format: {{"score": 8.5, "reason": "brief reason here"}}"""

    try:
        response = client.chat.completions.create(
            model="llama3.1:latest", # 使用本地 Llama-3.1 模型
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"},
            temperature=0.1
        )
        result = json.loads(response.choices[0].message.content)
        return float(result.get("score", 0)), result.get("reason", "Parse error")
    except Exception as e:
        print(f"LLM 评分出错: {e}")
        return 0.0, str(e)

def wash_data():
    if not os.path.exists(INPUT_FILE):
        print(f"❌ 找不到原始数据文件：{INPUT_FILE}")
        return

    print("🚀 开始数据清洗与伪标签 (Pseudo-labeling) 生成...")
    
    count = 0

    with open(INPUT_FILE, 'r', encoding='utf-8') as fin, \
         open(OUTPUT_SFT, 'w', encoding='utf-8') as fsft, \
         open(OUTPUT_DPO, 'w', encoding='utf-8') as fdpo:
        
        for line in fin:
            count += 1

            data = json.loads(line)
            
            # 找到最终的报告内容 (假设在 messages 的最后一条)
            messages = data.get("messages", [])
            final_report = messages[-1]["content"] if messages else ""
            
            # 1. 算硬性引用分
            cit_score, url_count = get_citation_score(final_report)
            
            if cit_score < 3.0:
                print(f"⚠️ [Task {data.get('id', count)}] 引用偏少({url_count}个)，但不提前淘汰，继续进行 LLM 结构打分...")

            # 2. 算结构分
            struct_score, reason = get_llm_structure_score(final_report)
            
            # 3. 综合得分 (引用占 40%，结构占 60%)
            final_score = (cit_score * 0.4) + (struct_score * 0.6)
            data["score"] = round(final_score, 2)
            data["reason"] = reason

            # 4. 分流保存
            if final_score >= 8.0:
                print(f"✅ [Task {data.get('id', count)}] 得分: {final_score}/10 -> 存入 SFT 训练集")
                fsft.write(json.dumps(data, ensure_ascii=False) + "\n")
            else:
                print(f"❌ [Task {data.get('id', count)}] 得分: {final_score}/10 -> 存入 DPO 负样本集 (结构分: {struct_score}, 理由: {reason})")
                fdpo.write(json.dumps(data, ensure_ascii=False) + "\n")
                
            time.sleep(1) # 给本地模型喘口气

if __name__ == "__main__":
    wash_data()
