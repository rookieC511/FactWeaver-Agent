import re
import os

LOG_FILE = "logs/gaia_full_run.log"

def parse_gaia_log(filepath):
    results = []
    current_task = {}
    
    try:
        try:
            with open(filepath, "r", encoding="utf-16le") as f:
                lines = f.readlines()
        except UnicodeError:
             with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
    except Exception as e:
        print(f"Error reading log: {e}")
        return

    for line in lines:
        line = line.strip()
        
        # New Task Start
        match_task = re.search(r"GAIA Level 2 \| Task: ([a-f0-9\-]+)", line)
        if match_task:
            if current_task:
                results.append(current_task)
            current_task = {"id": match_task.group(1), "question": "", "gold": "", "output": "", "status": "UNKNOWN", "reason": ""}
            continue
            
        if not current_task:
            continue

        # Question
        if line.startswith("Q:"):
            current_task["question"] = line[3:].strip()
        
        # Gold Answer
        elif line.startswith("Gold:"):
            current_task["gold"] = line[5:].strip()
            
        # Output
        elif line.startswith("Agent Output (truncated):"):
            current_task["output"] = line[26:].strip()
            
        # Exact Match
        elif line.startswith("[Exact Match]:"):
            em = line.split(":")[-1].strip()
            if em == "PASS":
                current_task["status"] = "PASS"
                current_task["reason"] = "Exact Match"
                
        # Judge Match
        elif line.startswith("[Judge Match]:"):
            jm = line.split(":")[-1].strip()
            if jm == "CORRECT":
                if current_task["status"] != "PASS":
                    current_task["status"] = "PASS"
                    current_task["reason"] = "Judge Approved"
            elif jm == "INCORRECT":
                if current_task["status"] != "PASS":
                    current_task["status"] = "FAIL"
                    current_task["reason"] = "Judge Rejected"

    if current_task:
        results.append(current_task)

    # Print Markdown Table
    print("| Task ID | Question | Gold Answer | Agent Answer | Status | Reasoning |")
    print("|---|---|---|---|---|---|")
    for r in results:
        # Deduplicate results if log has multiple runs? 
        # We take the last occurrence for each ID?
        pass # Handle later if needed. For now just print all found.
    
    # Filter unique by ID (last one wins)
    unique_results = {}
    for r in results:
        unique_results[r['id']] = r
        
    for tid, r in unique_results.items():
        q = r['question'][:50].replace("|", " ") + "..."
        g = r['gold'].replace("|", " ")
        o = r['output'].replace("|", " ")[:50] + "..."
        print(f"| {tid[:8]} | {q} | {g} | {o} | {r['status']} | {r['reason']} |")

if __name__ == "__main__":
    parse_gaia_log(LOG_FILE)
