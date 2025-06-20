import requests
import json
import time
import os
import sys
from typing import List, Dict, Optional, Any
from datetime import datetime
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ===============================================================
# 1. 从环境变量加载配置
# ===============================================================
BGM_USERNAME = os.getenv("BGM_USERNAME")
BGM_ACCESS_TOKEN = os.getenv("BGM_ACCESS_TOKEN")
BGM_USER_AGENT = os.getenv("BGM_USER_AGENT", "feng/bangumi2notion")
NOTION_API_KEY = os.getenv("NOTION_API_KEY")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID")
# 1 为 书籍
# 2 为 动画
# 3 为 音乐
# 4 为 游戏
# 6 为 三次元
# TARGET_SUBJECT_TYPE_CODE = 2
# TARGET_SUBJECT_TYPE_NAME = "动画"
TARGET_SUBJECT_TYPES = {
    2: "动画",
    6: "三次元"
}
TAG_LIMIT = 10

# ===============================================================
# 2. 配置带自动重试的全局 Session
# ===============================================================
# 定义重试策略
retry_strategy = Retry(
    total=3,  # 总重试次数
    backoff_factor=1,  # 重试间的等待时间因子，会以 0.5s, 1s, 2s... 的形式增长
    status_forcelist=[429, 500, 502, 503, 504],  # 对这些服务器错误状态码进行重试
    allowed_methods=["HEAD", "GET", "POST", "PATCH"] # 对这些请求方法启用重试
)
# 创建一个适配器，应用重试策略
adapter = HTTPAdapter(max_retries=retry_strategy)
# 创建一个 Session 对象
http_session = requests.Session()
# 将适配器挂载到 Session 上，对所有 https 和 http 的请求生效
http_session.mount("https://", adapter)
http_session.mount("http://", adapter)

# ===============================================================
# 3. Bangumi 数据模型
# ===============================================================
class Subject:
    def __init__(self, data: Dict[str, Any]):
        self.id: int = data.get('id')
        self.name: str = data.get('name', '')
        self.name_cn: str = data.get('name_cn', '')
        self.date: Optional[str] = data.get('date')
        self.images: Dict[str, str] = data.get('images', {})
        self.score: float = data.get('score', 0.0)
        self.eps: int = data.get('eps', 0)
        self.tags: List[Dict[str, Any]] = data.get('tags', [])
        self.short_summary: str = data.get('short_summary', '')

class ACG:
    def __init__(self, data: Dict[str, Any]):
        self.updated_at: str = data.get('updated_at', '')
        self.ep_status: int = data.get('ep_status', 0)
        self.type: int = data.get('type')
        self.rate: int = data.get('rate', 0)
        self.subject: Optional[Subject] = Subject(data.get('subject', {})) if data.get('subject') else None


# ===============================================================
# 4. Bangumi API 调用函数
# ===============================================================
def get_user_collection(username: str, access_token: str, subject_type: int, collection_type: int) -> List[ACG]:
    api_url = f"https://api.bgm.tv/v0/users/{username}/collections"
    headers = {'Authorization': f'Bearer {access_token}', 'User-Agent': BGM_USER_AGENT, 'Accept': 'application/json'}
    all_acg_objects = []
    offset = 0
    limit = 50
    while True:
        params = {'subject_type': subject_type, 'type': collection_type, 'limit': limit, 'offset': offset}
        try:
            response = http_session.get(api_url, headers=headers, params=params)
            response.raise_for_status()
            json_data = response.json()
            data_list = json_data.get('data', [])
            if not data_list: break
            for item_data in data_list:
                all_acg_objects.append(ACG(item_data))
            if len(data_list) < limit: break
            offset += limit
        except requests.exceptions.RequestException as e:
            print(f"[错误] 请求 Bangumi API 失败 (已重试): {e}")
            return []
    return all_acg_objects


# ===============================================================
# 5. Notion API 相关函数
# ===============================================================
NOTION_API_HEADERS = {
    'Authorization': f'Bearer {NOTION_API_KEY}',
    'Content-Type': 'application/json',
    'Notion-Version': '2022-06-28'
}

def find_notion_page_object_by_bgm_id(bgm_id: int) -> Optional[dict]:
    query_url = f"https://api.notion.com/v1/databases/{NOTION_DATABASE_ID}/query"
    query_payload = {"filter": {"property": "BGM ID", "number": {"equals": bgm_id}}}
    try:
        response = http_session.post(query_url, headers=NOTION_API_HEADERS, data=json.dumps(query_payload))
        response.raise_for_status()
        results = response.json().get('results', [])
        return results[0] if results else None
    except requests.exceptions.RequestException:
        return None

def is_update_required(acg_item: ACG, notion_page: dict, new_status: str, subject_type_name: str) -> bool:
    props = notion_page.get('properties', {})
    def get_notion_select(prop_name):
        select_obj = props.get(prop_name, {}).get('select')
        return select_obj.get('name') if select_obj else None
    def get_notion_number(prop_name):
        return props.get(prop_name, {}).get('number')
    def get_notion_multiselect(prop_name):
        return [tag['name'] for tag in props.get(prop_name, {}).get('multi_select', [])]
    def get_notion_richtext(prop_name):
        text_list = props.get(prop_name, {}).get('rich_text', [])
        return "".join([text['plain_text'] for text in text_list])

    if new_status != get_notion_select('状态'): return True
    # 新增对“类型”字段的检查，确保“动画”和“三次元”能被正确区分和更新
    if subject_type_name != get_notion_select('类型'): return True
    bgm_rate_str = str(acg_item.rate) if acg_item.rate > 0 else None
    if bgm_rate_str != get_notion_select('我的评分'): return True
    if acg_item.ep_status != get_notion_number('观看进度'): return True
    bgm_tags = set([tag['name'] for tag in acg_item.subject.tags[:TAG_LIMIT]])
    notion_tags = set(get_notion_multiselect('标签'))
    if bgm_tags != notion_tags: return True
    bgm_summary = acg_item.subject.short_summary.strip()
    notion_summary = get_notion_richtext('简介').strip()
    if bgm_summary != notion_summary: return True
    return False

def build_notion_properties(acg_item: ACG, status: str, subject_type_name: str) -> dict:
    properties = {
        "BGM ID": {"number": acg_item.subject.id},
        "标题": {"title": [{"text": {"content": acg_item.subject.name_cn or acg_item.subject.name}}]},
        "状态": {"select": {"name": status}},
        "类型": {"select": {"name": subject_type_name}},
        "BGM链接": {"url": f"https://bgm.tv/subject/{acg_item.subject.id}"},
        "最后同步": {"date": {"start": datetime.now().isoformat()}},
        "观看进度": {"number": acg_item.ep_status},
    }
    if acg_item.rate > 0:
        properties["我的评分"] = {"select": {"name": str(acg_item.rate)}}
    if acg_item.subject.score > 0:
        properties["BGM评分"] = {"number": acg_item.subject.score}
    if acg_item.subject.eps > 0:
        properties["总集数"] = {"number": acg_item.subject.eps}
    if acg_item.subject.date:
        properties["放送日期"] = {"date": {"start": acg_item.subject.date}}
    if acg_item.subject.images.get('large'):
        properties["封面"] = {"files": [{"name": acg_item.subject.images['large'], "type": "external", "external": {"url": acg_item.subject.images['large']}}]}
    if acg_item.subject.tags:
        tags_to_sync = [{"name": tag['name'][:100]} for tag in acg_item.subject.tags[:TAG_LIMIT]]
        properties["标签"] = {"multi_select": tags_to_sync}
    if acg_item.subject.short_summary:
        summary_content = acg_item.subject.short_summary[:2000]
        properties["简介"] = {"rich_text": [{"type": "text", "text": {"content": summary_content}}]}
    return properties

def create_notion_page(acg_item: ACG, status: str, subject_type_name: str):
    create_url = "https://api.notion.com/v1/pages"
    properties = build_notion_properties(acg_item, status, subject_type_name)
    payload = {"parent": {"database_id": NOTION_DATABASE_ID}, "properties": properties}
    try:
        response = http_session.post(create_url, headers=NOTION_API_HEADERS, data=json.dumps(payload))
        response.raise_for_status()
        print(f"  ✅ 成功创建页面: {acg_item.subject.name_cn or acg_item.subject.name}")
    except requests.exceptions.RequestException as e:
        print(f"  ❌ 创建页面失败: {acg_item.subject.name_cn or acg_item.subject.name}. 错误: {e.response.text}")

def update_notion_page(page_id: str, acg_item: ACG, status: str, subject_type_name: str):
    update_url = f"https://api.notion.com/v1/pages/{page_id}"
    properties = build_notion_properties(acg_item, status, subject_type_name)
    payload = {"properties": properties}
    try:
        response = http_session.patch(update_url, headers=NOTION_API_HEADERS, data=json.dumps(payload))
        response.raise_for_status()
        print(f"  🔄️ 成功更新页面: {acg_item.subject.name_cn or acg_item.subject.name}")
    except requests.exceptions.RequestException as e:
        print(f"  ❌ 更新页面失败: {acg_item.subject.name_cn or acg_item.subject.name}. 错误: {e.response.text}")

# ===============================================================
# 6. 主程序执行区
# ===============================================================
if __name__ == "__main__":
    required_secrets = {"BGM_USERNAME": BGM_USERNAME, "BGM_ACCESS_TOKEN": BGM_ACCESS_TOKEN, "NOTION_API_KEY": NOTION_API_KEY, "NOTION_DATABASE_ID": NOTION_DATABASE_ID}
    missing_secrets = [key for key, value in required_secrets.items() if not value]
    if missing_secrets:
        print("错误：以下必需的环境变量未设置，请在 GitHub Secrets 中配置：", missing_secrets)
        sys.exit(1)

    COLLECTION_TYPES = {"想看": 1, "看过": 2, "在看": 3, "搁置": 4, "抛弃": 5}
    
    # 将统计变量移到最外层，以便累计所有类型的总和
    total_new = 0
    total_updated = 0
    total_unchanged = 0

    for subject_code, subject_name in TARGET_SUBJECT_TYPES.items():
        print(f"\n{'='*20} 开始处理类别: {subject_name} {'='*20}")
        
        all_bgm_collections: Dict[str, List[ACG]] = {}
        for type_name, type_code in COLLECTION_TYPES.items():
            print(f"\n>>> 正在从 Bangumi 获取 '{subject_name}' 的 '{type_name}' 列表...")
            # 在 API 调用中使用当前的 subject_code
            collection_list = get_user_collection(BGM_USERNAME, BGM_ACCESS_TOKEN, subject_code, type_code)
            all_bgm_collections[type_name] = collection_list
            print(f">>> 获取完成，'{type_name}' 列表包含 {len(collection_list)} 个条目。")

        print(f"\n===================================")
        print(f"类别 '{subject_name}' 数据获取完毕，开始同步到 Notion...")
        print(f"===================================")

        for status, collection_list in all_bgm_collections.items():
            if not collection_list:
                continue
            print(f"\n--- 正在同步 '{status}' 列表 ({subject_name}) ---")
            for acg_item in collection_list:
                if not acg_item.subject:
                    continue
                
                existing_page_object = find_notion_page_object_by_bgm_id(acg_item.subject.id)
                
                if existing_page_object:
                    # 在调用时传入 subject_name
                    if is_update_required(acg_item, existing_page_object, status, subject_name):
                        update_notion_page(existing_page_object['id'], acg_item, status, subject_name)
                        total_updated += 1
                    else:
                        print(f"  👍 无需变动: {acg_item.subject.name_cn or acg_item.subject.name}")
                        total_unchanged += 1
                else:
                    # 在调用时传入 subject_name
                    create_notion_page(acg_item, status, subject_name)
                    total_new += 1
                
                # Bangumi 和 Notion API 都有速率限制，保留适当的延时
                time.sleep(0.4)

    print("\n\n===================================")
    print("所有类别同步完成！")
    print(f"✅ 新增条目: {total_new}")
    print(f"🔄️ 更新条目: {total_updated}")
    print(f"👍 无需变动: {total_unchanged}")
    print("===================================")