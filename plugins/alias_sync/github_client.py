"""GitHub Contents API 轻量客户端（无需 git 二进制）"""
import base64
import json
import time
import urllib.error
import urllib.request
from typing import Any, Optional

API_BASE = "https://api.github.com"


class GitHubError(Exception):
    """GitHub API 调用失败"""


class GitHubClient:
    def __init__(
        self,
        repo: str,
        token: str,
        branch: str = "main",
        timeout: int = 30,
        author: str = "NympheaBot",
    ):
        self.repo = repo.strip().strip("/")
        self.token = token.strip()
        self.branch = branch
        self.timeout = timeout
        self.author = author

    # ---------- 内部 ----------

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "nymphea-bot-alias-sync",
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        url = f"{API_BASE}/repos/{self.repo}/{path.lstrip('/')}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            raise GitHubError(f"HTTP {e.code} {url} :: {detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise GitHubError(f"网络错误 {url} :: {e!r}") from e

    @staticmethod
    def _author_block() -> dict:
        now = time.strftime("%Y-%m-%dT%H:%M:%S+08:00")
        return {"name": "NympheaBot", "email": "nymphea@users.noreply.github.com", "date": now}

    # ---------- 公开 ----------

    def get_file(self, path: str) -> tuple[Optional[str], Optional[str]]:
        """读取文件。返回 (内容, sha)；文件不存在返回 (None, None)。"""
        q = f"?ref={self.branch}"
        try:
            data = self._request("GET", f"contents/{path}{q}")
        except GitHubError as e:
            if "HTTP 404" in str(e):
                return None, None
            raise
        content_b64 = data.get("content") or ""
        sha = data.get("sha")
        if not content_b64:
            return "", sha
        content = base64.b64decode(content_b64).decode("utf-8")
        return content, sha

    def put_file(self, path: str, content: str, message: str, sha: Optional[str]) -> str:
        """创建/更新文件，返回 commit sha。"""
        payload: dict[str, Any] = {
            "message": message,
            "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
            "branch": self.branch,
            "committer": self._author_block(),
            "author": self._author_block(),
        }
        if sha:
            payload["sha"] = sha
        data = self._request("PUT", f"contents/{path}", payload)
        return data.get("commit", {}).get("sha", "")
