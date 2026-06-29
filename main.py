#!/usr/bin/env python3
"""xbot gRPC channel 插件：GitLab Code Reviewer

当 GitLab Bot 被 @mention 时，自动对所在 MR 进行 Code Review 并评论。
默认 Poll 模式，无需公网 IP 或 webhook 配置。
"""

import sys
import os
import json
import time
import logging
import threading
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from gitlab_client import GitLabAuth, GitLabClient, GITLAB_API_DEFAULT

# ---- 全局状态 ----

CONFIG: dict = {}
GITLAB_CLIENT: GitLabClient | None = None
BOT_USERNAME: str = "code-reviewer-bot"

# 持久化
_PROCESSED_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "processed_comments.json")
_PROCESSED_LOCK = threading.Lock()
PROCESSED_COMMENTS: set[int] = set()

# 启动时间戳（防洪水）
_startup_time: float = 0.0
# PR 缓存（ETag 304 时使用）
_repo_prs_cache: dict[str, list[int]] = {}

logging.basicConfig(stream=sys.stderr, level=logging.DEBUG,
                     format="[glbot] %(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("glbot")

# ---- 持久化 ----

def _load_processed():
    global PROCESSED_COMMENTS
    try:
        if os.path.exists(_PROCESSED_FILE):
            with open(_PROCESSED_FILE) as f:
                PROCESSED_COMMENTS = set(json.load(f).get("ids", []))
                log.info("从文件加载 %d 条已处理评论", len(PROCESSED_COMMENTS))
    except Exception as e:
        log.warning("加载失败: %s", e)

def _save_processed():
    try:
        if len(PROCESSED_COMMENTS) > 10000:
            ids = sorted(PROCESSED_COMMENTS)[-10000:]
            PROCESSED_COMMENTS.clear()
            PROCESSED_COMMENTS.update(ids)
        with open(_PROCESSED_FILE, "w") as f:
            json.dump({"ids": list(PROCESSED_COMMENTS)}, f)
    except Exception as e:
        log.warning("保存失败: %s", e)

def _try_mark_processed(comment_id: int) -> bool:
    with _PROCESSED_LOCK:
        if comment_id in PROCESSED_COMMENTS:
            return False
        PROCESSED_COMMENTS.add(comment_id)
        _save_processed()
        return True

# ---- JSON-RPC ----

def write_stdout(obj: dict):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()

_msg_counter = 0

def next_id() -> str:
    global _msg_counter
    _msg_counter += 1
    return f"glbot-{_msg_counter}-{int(time.time()*1000)%1000000}"

def send_inbound(chat_id: str, content: str, sender_name: str, sender_id: str = ""):
    write_stdout({
        "id": next_id(), "method": "send_inbound",
        "params": {"channel": "gitlab", "chat_id": chat_id, "content": content,
                    "sender_id": sender_id or chat_id, "sender_name": sender_name, "chat_type": "group"}
    })

# ---- 过滤 ----

def parse_list(value: str) -> list[str]:
    if not value:
        return []
    return [i.strip().lower().strip("@") for i in value.split(",") if i.strip()]

def is_allowed(project_path: str, username: str) -> tuple[bool, str]:
    """白名单/黑名单，支持 org/* 通配符。"""
    p = project_path.lower()
    u = username.lower()
    bl = parse_list(CONFIG.get("blacklist_repos", ""))
    if p in bl:
        return False, f"项目 {project_path} 在黑名单中"
    bl_u = parse_list(CONFIG.get("blacklist_users", ""))
    if u in bl_u:
        return False, f"用户 @{username} 在黑名单中"
    wl = parse_list(CONFIG.get("whitelist_repos", ""))
    if wl:
        for pat in wl:
            if pat.endswith("/*"):
                if p.startswith(pat[:-2] + "/"):
                    break
            elif pat == p:
                break
        else:
            return False, f"项目 {project_path} 不在白名单中"
    wl_u = parse_list(CONFIG.get("whitelist_users", ""))
    if wl_u and u not in wl_u:
        return False, f"用户 @{username} 不在白名单中"
    return True, "ok"

def is_mentioned(text: str) -> bool:
    if not text:
        return False
    u = BOT_USERNAME.lower().lstrip("@")
    return bool(re.search(rf"@{re.escape(u)}\b", text, re.IGNORECASE))

# ---- Tools 声明 ----

def declare_tools():
    write_stdout({"type": "channel_tools", "tools": [
        {"name":"get_mr_info","description":"获取 MR 基本信息","parameters":[
            {"name":"project","type":"string","description":"项目路径 group/project","required":True},
            {"name":"mr_iid","type":"integer","description":"MR 编号","required":True}]},
        {"name":"get_mr_changes","description":"获取 MR 变更详情（含 diff 和 diff_refs，用于行级评论）","parameters":[
            {"name":"project","type":"string","description":"项目路径","required":True},
            {"name":"mr_iid","type":"integer","description":"MR 编号","required":True}]},
        {"name":"post_mr_review","description":"发表 MR 整体评论 + 行级 discussion","parameters":[
            {"name":"project","type":"string","description":"项目路径","required":True},
            {"name":"mr_iid","type":"integer","description":"MR 编号","required":True},
            {"name":"body","type":"string","description":"整体总结","required":True},
            {"name":"line_comments","type":"array","description":"行级评论列表 [{file_path, new_line, body}]","required":False}]},
        {"name":"post_mr_note","description":"在 MR 上发表普通评论","parameters":[
            {"name":"project","type":"string","description":"项目路径","required":True},
            {"name":"mr_iid","type":"integer","description":"MR 编号","required":True},
            {"name":"body","type":"string","description":"评论内容","required":True}]},
    ]})

# ---- Tool 执行 ----

def execute_tool(name: str, input_str: str) -> tuple[str, bool]:
    global GITLAB_CLIENT
    if not GITLAB_CLIENT:
        return "客户端未初始化", True
    try:
        params = json.loads(input_str)
    except json.JSONDecodeError as e:
        return f"JSON 解析失败: {e}", True

    project = params.get("project", "")
    mr_iid = params.get("mr_iid", 0)
    if not project or not mr_iid:
        return "缺少 project 或 mr_iid", True

    try:
        if name == "get_mr_info":
            info = GITLAB_CLIENT.get_mr_info(project, mr_iid)
            return json.dumps(info, ensure_ascii=False, indent=2), False
        elif name == "get_mr_changes":
            changes = GITLAB_CLIENT.get_mr_changes(project, mr_iid)
            return json.dumps(changes, ensure_ascii=False, indent=2), False
        elif name == "post_mr_review":
            body = params.get("body", "")
            line_comments = params.get("line_comments", [])
            info = GITLAB_CLIENT.get_mr_info(project, mr_iid)
            diff_refs = info["diff_refs"]
            result = GITLAB_CLIENT.post_mr_review(project, mr_iid, body, diff_refs, line_comments if line_comments else None)
            return f"✅ 已发表 ({len(result['line_results'])} 条行级评论)", False
        elif name == "post_mr_note":
            body = params.get("body", "")
            GITLAB_CLIENT.post_note(project, mr_iid, body)
            return "✅ 已发表", False
        else:
            return f"未知工具: {name}", True
    except Exception as e:
        log.exception("工具执行失败: %s", name)
        return f"API 调用失败: {e}", True

# ---- CR 触发 ----

def has_bot_reviewed_before(project_path: str, mr_iid: int) -> bool:
    if not GITLAB_CLIENT:
        return False
    try:
        notes = GITLAB_CLIENT.get_mr_notes(project_path, mr_iid)
        if notes is None:
            return True  # ETag 304，保守认为已 review 过
        for n in notes:
            if BOT_USERNAME.lower() in n["author"].lower():
                return True
        return False
    except Exception:
        return False

def trigger_cr(project_path: str, mr_iid: int, note_body: str, author: str, note_id: int):
    if not _try_mark_processed(note_id):
        log.info("Note %d 已处理过，跳过", note_id)
        return
    log.info("CR 请求: %s#%d by @%s", project_path, mr_iid, author)

    chat_id = f"{project_path}#mr-{mr_iid}"
    mr_summary = ""
    try:
        if GITLAB_CLIENT:
            info = GITLAB_CLIENT.get_mr_info(project_path, mr_iid)
            mr_summary = (
                f"**标题:** {info.get('title', 'N/A')}\n"
                f"**作者:** @{info.get('author', 'N/A')}\n"
                f"**分支:** {info.get('source_branch', '?')} → {info.get('target_branch', '?')}\n"
            )
    except Exception as e:
        log.warning("获取 MR 信息失败: %s", e)

    is_rereview = has_bot_reviewed_before(project_path, mr_iid)
    if is_rereview:
        content = _build_rereview_prompt(project_path, mr_iid, mr_summary, author, note_body)
    else:
        content = _build_first_prompt(project_path, mr_iid, mr_summary, author, note_body)
    send_inbound(chat_id, content, f"@{author}")

def _build_first_prompt(project, mr_iid, mr_summary, author, note_body):
    return (
        f"## 🔍 Code Review 请求\n\n"
        f"**项目:** `{project}`\n"
        f"**MR:** !{mr_iid}\n"
        f"{mr_summary}\n"
        f"**触发者:** @{author}\n"
        f"**评论:** {note_body}\n\n"
        f"---\n\n"
        f"请对这个 MR 进行 Code Review。\n\n"
        f"**步骤:**\n"
        f"1. 使用 `get_mr_changes` 获取 diff 和 diff_refs\n"
        f"2. 审查代码：Bug、安全风险、性能问题\n"
        f"3. 使用 `post_mr_review` 发表评审（整体总结 + 行级评论）\n\n"
        f"**严格规则:**\n"
        f"- 只评论有实际问题的代码行，禁止发表正面评价、良好实践、命名清晰等\n"
        f"- 行级评论按严重程度标注：🔴 严重 / 🟡 建议\n"
        f"- 有实质问题 → REQUEST_CHANGES；无问题 → APPROVE"
    )

def _build_rereview_prompt(project, mr_iid, mr_summary, author, note_body):
    return (
        f"## 🔄 重新 Code Review 请求\n\n"
        f"**项目:** `{project}`\n"
        f"**MR:** !{mr_iid}\n"
        f"{mr_summary}\n"
        f"**触发者:** @{author}\n"
        f"**评论:** {note_body}\n\n"
        f"---\n\n"
        f"用户已修改代码，请重新审查，重点关注之前的问题是否已修复。\n\n"
        f"**步骤:**\n"
        f"1. 使用 `get_mr_changes` 获取最新 diff\n"
        f"2. 检查之前的问题是否已修复\n"
        f"3. 使用 `post_mr_review` 发表评审\n\n"
        f"**严格规则:**\n"
        f"- 只评论仍有实际问题的代码行\n"
        f"- 已修复的问题不需要评论\n"
        f"- 行级评论标注：🔴 严重 / 🟡 建议\n"
        f"- 全部修复且无新问题 → APPROVE；仍有问题 → REQUEST_CHANGES"
    )

def should_process_note(note_body, author, project_path, note_id):
    if author.lower() == BOT_USERNAME.lower():
        return False, "bot 自身"
    if note_id in PROCESSED_COMMENTS:
        return False, "已处理过"
    if not is_mentioned(note_body):
        return False, "未 @mention"
    allowed, reason = is_allowed(project_path, author)
    if not allowed:
        return False, reason
    return True, "ok"

# ---- 轮询 ----

def get_monitored_projects() -> list[str]:
    configured = parse_list(CONFIG.get("monitored_repos", ""))
    if configured:
        return list(dict.fromkeys(configured))
    projects = set()
    if GITLAB_CLIENT:
        try:
            for p in GITLAB_CLIENT.list_projects():
                projects.add(p["path_with_namespace"])
            log.info("自动发现 %d 个项目", len(projects))
        except Exception as e:
            log.error("发现项目失败: %s", e)
    wl = parse_list(CONFIG.get("whitelist_repos", ""))
    for r in wl:
        if not r.endswith("/*"):
            projects.add(r)
    return list(projects)

def _scan_one_project(project: str) -> list[tuple]:
    triggers = []
    if not GITLAB_CLIENT:
        return triggers
    try:
        mrs = GITLAB_CLIENT.list_open_mrs(project)
        if mrs is None:
            mr_nums = _repo_prs_cache.get(project, [])
            if not mr_nums:
                return triggers
        else:
            mr_nums = [m["iid"] for m in mrs]
            _repo_prs_cache[project] = mr_nums
            if not mr_nums:
                return triggers

        def fetch_notes(mr_iid):
            try:
                return GITLAB_CLIENT.get_mr_notes(project, mr_iid)
            except Exception:
                return []
        with ThreadPoolExecutor(max_workers=min(8, len(mr_nums))) as ex:
            futures = {ex.submit(fetch_notes, n): n for n in mr_nums}
            for fut in as_completed(futures):
                n = futures[fut]
                notes = fut.result()
                if notes is None:
                    continue
                for note in notes:
                    ok, reason = should_process_note(note["body"], note["author"], project, note["id"])
                    if not ok:
                        if reason not in ("未 @mention", "bot 自身", "已处理过"):
                            log.debug("跳过 %d: %s", note["id"], reason)
                        continue
                    if _startup_time > 0 and note.get("created_at"):
                        from datetime import datetime, timezone as tz
                        try:
                            dt = datetime.fromisoformat(note["created_at"].replace("Z", "+00:00"))
                            if dt.timestamp() < _startup_time:
                                _try_mark_processed(note["id"])
                                continue
                        except (ValueError, TypeError):
                            pass
                    triggers.append((project, n, note["body"], note["author"], note["id"]))
    except Exception as e:
        log.warning("扫描项目失败: %s: %s", project, e)
    return triggers

def _scan_comments():
    projects = get_monitored_projects()
    if not projects:
        return
    results = []
    with ThreadPoolExecutor(max_workers=min(20, len(projects))) as ex:
        fm = {ex.submit(_scan_one_project, p): p for p in projects}
        for fut in as_completed(fm):
            try:
                results.extend(fut.result())
            except Exception as e:
                log.warning("异常: %s", e)
    for project, mr_iid, note_body, author, note_id in results:
        trigger_cr(project, mr_iid, note_body, author, note_id)

def poll_loop(interval: int):
    global _startup_time
    _startup_time = time.time()
    log.info("轮询启动，间隔 %ds", interval)
    while True:
        try:
            _scan_comments()
            time.sleep(interval)
        except Exception:
            log.exception("轮询异常")

# ---- 主循环 ----

def main():
    global CONFIG, GITLAB_CLIENT, BOT_USERNAME
    log.info("等待 xbot 消息...")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        method = msg.get("method", "")
        mtype = msg.get("type", "")
        mid = msg.get("id", "")

        if method == "activate":
            write_stdout({"result": "ok", "channel_provider": {
                "name": "gitlab", "config_schema": [
                    {"key":"enabled","label":"启用","type":"toggle","default_value":"false"},
                    {"key":"mode","label":"模式","type":"select","options":["poll"],"default_value":"poll"},
                    {"key":"gitlab_url","label":"GitLab URL","type":"text","default_value":"https://gitlab.com"},
                    {"key":"pat_token","label":"Personal Access Token","type":"password","default_value":""},
                    {"key":"bot_username","label":"Bot 用户名","type":"text","default_value":"code-reviewer-bot"},
                    {"key":"poll_interval","label":"轮询间隔(秒)","type":"number","default_value":"10"},
                    {"key":"monitored_repos","label":"监控项目","type":"text","default_value":""},
                    {"key":"whitelist_repos","label":"项目白名单","type":"text","default_value":""},
                    {"key":"blacklist_repos","label":"项目黑名单","type":"text","default_value":""},
                    {"key":"whitelist_users","label":"用户白名单","type":"text","default_value":""},
                    {"key":"blacklist_users","label":"用户黑名单","type":"text","default_value":""},
                ]}})
            continue

        if mtype == "channel_config":
            raw = msg.get("metadata", {}).get("config", "{}")
            CONFIG = json.loads(raw) if isinstance(raw, str) else (raw or {})
            if CONFIG.get("enabled","false") != "true":
                continue
            token = CONFIG.get("pat_token", "")
            if not token:
                log.error("缺少 pat_token")
                continue
            gitlab_url = CONFIG.get("gitlab_url", GITLAB_API_DEFAULT).rstrip("/") + "/api/v4"
            verify_ssl = "gitlab.com" in gitlab_url  # 自建实例通常为自签证书
            GITLAB_CLIENT = GitLabClient(GitLabAuth(token), gitlab_url, verify_ssl=verify_ssl)
            BOT_USERNAME = CONFIG.get("bot_username", "code-reviewer-bot")
            _load_processed()
            declare_tools()
            interval = int(CONFIG.get("poll_interval", "10"))
            threading.Thread(target=poll_loop, args=(interval,), daemon=True).start()
            log.info("Poll 模式启动，Bot=%s", BOT_USERNAME)
            continue

        if method == "execute_tool":
            tn = msg.get("params", {}).get("name", "")
            ti = msg.get("params", {}).get("input", "{}")
            content, is_err = execute_tool(tn, ti)
            write_stdout({"id": mid, "result": {"content": content, "is_error": is_err}})
            continue

        if mtype in ("text", "stream_content", "session", "progress_structured"):
            continue
        if method == "channel_send":
            write_stdout({"id": mid, "result": "ok"})
            continue

if __name__ == "__main__":
    main()
