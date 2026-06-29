#!/usr/bin/env python3
"""GitLab API 封装。

认证: Personal Access Token (PRIVATE-TOKEN header)
API v4: https://gitlab.com/api/v4/ 或自建实例

支持：
  - 列出可访问项目
  - 列出 open MR
  - 获取 MR notes / changes / diff refs
  - 发表普通评论、行级 discussion
"""

import time
import requests
from urllib.parse import quote_plus

GITLAB_API_DEFAULT = "https://gitlab.com/api/v4"


class GitLabAuth:
    """GitLab 认证：PAT token 管理（简单模式，无过期刷新）。"""

    def __init__(self, token: str):
        self.token = token


class GitLabClient:
    """GitLab API 客户端。"""

    def __init__(self, auth: GitLabAuth, base_url: str = GITLAB_API_DEFAULT, verify_ssl: bool = True):
        self.auth = auth
        self.base_url = base_url.rstrip("/")
        self.verify_ssl = verify_ssl
        # ETag 缓存
        self._etags: dict[str, str] = {}

    def _headers(self, accept: str = "application/json") -> dict:
        return {
            "PRIVATE-TOKEN": self.auth.token,
            "Accept": accept,
        }

    def _project_id(self, project_path: str) -> str:
        """将 project path 转为 URL-encoded ID。"""
        return quote_plus(project_path, safe="")

    # ---- 项目相关 ----

    def list_projects(self) -> list[dict]:
        """列出当前 token 可访问的所有项目。"""
        url = f"{self.base_url}/projects"
        params = {"membership": "true", "per_page": 100, "order_by": "last_activity_at"}
        projects = []
        page = 1
        while True:
            params["page"] = page
            resp = requests.get(url, params=params, headers=self._headers(), timeout=15, verify=self.verify_ssl)
            if resp.status_code != 200:
                break
            data = resp.json()
            for p in data:
                projects.append({
                    "id": p["id"],
                    "path_with_namespace": p["path_with_namespace"],
                    "name": p["name"],
                })
            if len(data) < 100:
                break
            page += 1
        return projects

    # ---- MR 相关 ----

    def list_open_mrs(self, project_path: str) -> list[dict] | None:
        """列出项目的 open MR。使用 ETag 条件请求。"""
        pid = self._project_id(project_path)
        url = f"{self.base_url}/projects/{pid}/merge_requests"
        params = {"state": "opened", "per_page": 50, "sort": "updated_desc"}
        headers = self._headers()
        etag = self._etags.get(f"mrs:{project_path}")
        if etag:
            headers["If-None-Match"] = etag

        resp = requests.get(url, params=params, headers=headers, timeout=15, verify=self.verify_ssl)
        if resp.status_code == 304:
            return None

        new_etag = resp.headers.get("ETag")
        if new_etag:
            self._etags[f"mrs:{project_path}"] = new_etag

        if resp.status_code != 200:
            return []

        result = []
        for mr in resp.json():
            result.append({
                "iid": mr["iid"],
                "title": mr["title"],
                "author": mr.get("author", {}).get("username", ""),
                "updated_at": mr.get("updated_at"),
                "state": mr.get("state"),
                "draft": mr.get("draft", False),
            })
        return result

    def get_mr_info(self, project_path: str, mr_iid: int) -> dict:
        """获取 MR 详情。"""
        pid = self._project_id(project_path)
        url = f"{self.base_url}/projects/{pid}/merge_requests/{mr_iid}"
        resp = requests.get(url, headers=self._headers(), timeout=15, verify=self.verify_ssl)
        resp.raise_for_status()
        mr = resp.json()
        return {
            "iid": mr["iid"],
            "title": mr["title"],
            "description": (mr.get("description") or "")[:3000],
            "state": mr.get("state"),
            "author": mr.get("author", {}).get("username", ""),
            "source_branch": mr.get("source_branch"),
            "target_branch": mr.get("target_branch"),
            "web_url": mr.get("web_url"),
            "diff_refs": mr.get("diff_refs", {}),
            "draft": mr.get("draft", False),
        }

    def get_mr_notes(self, project_path: str, mr_iid: int) -> list[dict] | None:
        """获取 MR 的 notes（按时间正序）。使用 ETag。"""
        pid = self._project_id(project_path)
        url = f"{self.base_url}/projects/{pid}/merge_requests/{mr_iid}/notes"
        params = {"sort": "asc", "per_page": 100}
        headers = self._headers()
        etag = self._etags.get(f"notes:{project_path}:{mr_iid}")
        if etag:
            headers["If-None-Match"] = etag

        resp = requests.get(url, params=params, headers=headers, timeout=15, verify=self.verify_ssl)
        if resp.status_code == 304:
            return None

        new_etag = resp.headers.get("ETag")
        if new_etag:
            self._etags[f"notes:{project_path}:{mr_iid}"] = new_etag

        if resp.status_code != 200:
            return []

        result = []
        for n in resp.json():
            if n.get("system"):  # 跳过系统生成的 note
                continue
            result.append({
                "id": n["id"],
                "body": n.get("body", ""),
                "author": n.get("author", {}).get("username", ""),
                "created_at": n.get("created_at"),
                "updated_at": n.get("updated_at"),
            })
        return result

    def get_mr_changes(self, project_path: str, mr_iid: int) -> dict:
        """获取 MR 的 changes（diff + 文件列表）。"""
        pid = self._project_id(project_path)
        url = f"{self.base_url}/projects/{pid}/merge_requests/{mr_iid}/changes"
        resp = requests.get(url, headers=self._headers(), timeout=30, verify=self.verify_ssl)
        resp.raise_for_status()
        data = resp.json()
        files = []
        for f in data.get("changes", []):
            patch = f.get("diff", "")
            files.append({
                "old_path": f.get("old_path"),
                "new_path": f.get("new_path"),
                "new_file": f.get("new_file", False),
                "deleted_file": f.get("deleted_file", False),
                "renamed_file": f.get("renamed_file", False),
                "patch": patch[:5000],
                "patch_truncated": len(patch) > 5000,
            })
        return {
            "files": files,
            "diff_refs": data.get("diff_refs", {}),
        }

    # ---- 发表评论 ----

    def post_note(self, project_path: str, mr_iid: int, body: str) -> dict:
        """在 MR 上发表普通评论（note）。"""
        pid = self._project_id(project_path)
        url = f"{self.base_url}/projects/{pid}/merge_requests/{mr_iid}/notes"
        resp = requests.post(url, json={"body": body}, headers=self._headers(), timeout=15, verify=self.verify_ssl)
        resp.raise_for_status()
        return resp.json()

    def post_line_discussion(
        self,
        project_path: str,
        mr_iid: int,
        diff_refs: dict,
        body: str,
        file_path: str,
        new_line: int,
        old_line: int | None = None,
    ) -> dict:
        """在 MR 特定文件的特定行上发表行级 discussion。

        Args:
            diff_refs: MR 的 diff refs（从 get_mr_info 或 get_mr_changes 获取）
            file_path: 文件路径（new_path）
            new_line: 新文件的行号
            old_line: 旧文件的行号（可选）
        """
        pid = self._project_id(project_path)
        url = f"{self.base_url}/projects/{pid}/merge_requests/{mr_iid}/discussions"
        position = {
            "base_sha": diff_refs.get("base_sha", ""),
            "start_sha": diff_refs.get("start_sha", ""),
            "head_sha": diff_refs.get("head_sha", ""),
            "position_type": "text",
            "new_path": file_path,
            "new_line": new_line,
        }
        if old_line:
            position["old_path"] = file_path
            position["old_line"] = old_line

        payload = {"body": body, "position": position}
        resp = requests.post(url, json=payload, headers=self._headers(), timeout=20, verify=self.verify_ssl)
        resp.raise_for_status()
        return resp.json()

    def post_mr_review(
        self,
        project_path: str,
        mr_iid: int,
        body: str,
        diff_refs: dict,
        line_comments: list[dict] | None = None,
    ) -> dict:
        """发表 MR 整体 review + 行级评论。

        Args:
            body: 整体总结
            diff_refs: MR diff refs
            line_comments: [{file_path, new_line, old_line?, body}]
        """
        results = {"body": body, "line_results": []}

        # 先发整体评论
        try:
            note = self.post_note(project_path, mr_iid, body)
            results["note"] = note
        except Exception as e:
            results["note_error"] = str(e)

        # 再发行级评论
        if line_comments:
            for lc in line_comments:
                try:
                    disc = self.post_line_discussion(
                        project_path, mr_iid, diff_refs,
                        body=lc["body"],
                        file_path=lc["file_path"],
                        new_line=lc.get("new_line", 1),
                        old_line=lc.get("old_line"),
                    )
                    results["line_results"].append({"success": True, "discussion": disc})
                except Exception as e:
                    results["line_results"].append({"success": False, "error": str(e), "comment": lc})

        return results
