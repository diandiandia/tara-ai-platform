import csv
import json
import sqlite3
import httpx
import time
import sys

def main():
    # 1. Load settings from DB
    try:
        conn = sqlite3.connect('../data/tara.db')
        cursor = conn.cursor()
        cursor.execute('SELECT api_base_url, api_key, model_name FROM system_settings LIMIT 1')
        row = cursor.fetchone()
        if not row:
            print("Error: No system settings found in tara.db")
            sys.exit(1)
        api_base_url, api_key, model_name = row
        conn.close()
    except Exception as e:
        print(f"Error loading system settings from database: {e}")
        sys.exit(1)

    # 2. Read input CSV
    csv_path = '/mnt/d/Downloads/data1.csv'
    try:
        with open(csv_path, encoding='utf-8-sig') as f:
            rows = list(csv.reader(f))
    except Exception as e:
        print(f"Error reading CSV file at {csv_path}: {e}")
        sys.exit(1)

    # Extract raw requirements (all 188 rows)
    raw_reqs = []
    for idx, r in enumerate(rows):
        if len(r) >= 2:
            raw_reqs.append({
                "row_idx": idx,
                "id": r[0].strip(),
                "desc": r[1].strip()
            })

    # Extract summarized requirements (first 84 rows from Col 6)
    sum_reqs = []
    for idx, r in enumerate(rows):
        if idx < 84 and len(r) > 6 and r[6].strip():
            sum_reqs.append({
                "index": idx + 1,  # 1-based index
                "desc": r[6].strip()
            })

    print(f"Loaded {len(raw_reqs)} raw requirements.")
    print(f"Loaded {len(sum_reqs)} summarized requirements.")

    # Prepare summarized requirements list text
    sum_list_text = ""
    for sr in sum_reqs:
        sum_list_text += f"{sr['index']}: {sr['desc']}\n"

    # 3. Call LLM to map in batches of 15
    mapping = {}  # raw_id -> list of sum_indexes
    client = httpx.Client(timeout=90.0)

    batch_size = 15
    for i in range(0, len(raw_reqs), batch_size):
        batch = raw_reqs[i:i+batch_size]
        print(f"Processing batch {i//batch_size + 1}/{(len(raw_reqs)+batch_size-1)//batch_size}...")
        
        batch_text = ""
        for rr in batch:
            batch_text += f"ID: {rr['id']}\n描述: {rr['desc']}\n---\n"
            
        system_prompt = (
            "你是一个汽车网络安全合规专家。你的任务是将底层的原始安全需求（Raw Requirements）"
            "映射到合并归纳后的高层安全需求（Summarized Requirements）上。\n"
            "请返回 JSON 格式结果，格式如下：\n"
            "{\n"
            "  \"原始ID\": [匹配的合并需求序号1, 匹配的合并需求序号2],\n"
            "  ...\n"
            "}\n"
            "注意：如果没有匹配的合并需求，对应的数组返回空数组 []。请只返回 JSON 对象，不要包含 markdown 代码块或其他任何文字。"
        )
        
        user_prompt = (
            f"【合并归纳安全需求列表 (Summarized Requirements)】:\n{sum_list_text}\n\n"
            f"【待映射的原始安全需求列表 (Raw Requirements)】:\n{batch_text}\n"
        )
        
        url = f"{api_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json"
        }
        
        payload = {
            "model": model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "response_format": {"type": "json_object"}
        }
        
        # Retry logic
        success = False
        for attempt in range(5):
            try:
                resp = client.post(url, headers=headers, json=payload)
                resp.raise_for_status()
                res_json = resp.json()
                content = res_json["choices"][0]["message"]["content"]
                # Clean possible markdown wrap
                content_cleaned = content.strip()
                if content_cleaned.startswith("```"):
                    content_cleaned = content_cleaned.split("\n", 1)[1]
                    if content_cleaned.endswith("```"):
                        content_cleaned = content_cleaned.rsplit("\n", 1)[0]
                
                batch_map = json.loads(content_cleaned)
                mapping.update(batch_map)
                success = True
                break
            except Exception as e:
                print(f"Attempt {attempt+1} failed: {e}")
                time.sleep(2)
                
        if not success:
            print(f"Failed to process batch starting at index {i}")

    # 4. Compile trace mapping
    # For each summarized requirement, find all raw requirements mapped to it
    csr_sum_mapping = {sr['index']: [] for sr in sum_reqs}
    for raw_id, sum_indices in mapping.items():
        if not isinstance(sum_indices, list):
            continue
        for s_idx in sum_indices:
            # Ensure index exists in mapping
            if s_idx in csr_sum_mapping:
                # find raw desc
                raw_desc = next((rr['desc'] for rr in raw_reqs if rr['id'] == raw_id), "")
                csr_sum_mapping[s_idx].append((raw_id, raw_desc))

    # 5. Output mapped CSV
    output_path = '/mnt/d/Downloads/data1_mapped.csv'
    try:
        with open(output_path, 'w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f)
            # Header
            writer.writerow([
                "合并后安全需求ID / Summarized CSR ID",
                "合并后安全需求要求 / Summarized CSR Requirement",
                "追溯原始安全要求ID / Traced Raw Requirement IDs",
                "追溯原始安全要求描述 / Traced Raw Requirement Descriptions"
            ])
            
            for sr in sum_reqs:
                s_idx = sr['index']
                desc = sr['desc']
                
                # Mapped raw info
                mapped_raws = csr_sum_mapping[s_idx]
                raw_ids_str = ", ".join(sorted(list(set(item[0] for item in mapped_raws))))
                
                # Build a multiline string for descriptions
                raw_descs_str = "\n".join(f"[{item[0]}] {item[1]}" for item in mapped_raws)
                
                # Write row
                writer.writerow([
                    f"CSR_SUM_{s_idx:03d}",
                    desc,
                    raw_ids_str,
                    raw_descs_str
                ])
        print(f"Success! Mapped CSV written to {output_path}")
    except Exception as e:
        print(f"Error writing output CSV file: {e}")

if __name__ == '__main__':
    main()
