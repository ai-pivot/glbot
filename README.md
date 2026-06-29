# glbot — GitLab Code Reviewer for xbot

> 当有人在 MR 中 **@你的 Bot** 时，自动拉取 diff 进行 AI Code Review。

## ✨ 功能

- 🔔 **@mention 触发**：MR 评论中 @Bot 即可触发 CR
- 📍 **行级评论**：评审精确到代码行
- 🔄 **重新审查**：修改后再次 @Bot，自动检查修复情况
- 🚦 **APPROVE / REQUEST_CHANGES**：根据代码质量选择结论
- 🛡️ **白名单/黑名单**：项目级 + 用户级，支持 `org/*` 通配符
- 🔁 **Poll 模式**：无需公网 IP，10 秒内响应
- 📝 **持久化防重复**：同一评论只触发一次

## 📋 前置条件

- Python 3.10+
- xbot 运行中
- GitLab Personal Access Token（`api` + `read_user` 权限）

## 🚀 安装

```bash
mkdir -p ~/.xbot/plugins/my.glbots
cd ~/.xbot/plugins/my.glbots
git clone https://github.com/ai-pivot/glbot.git .
pip install -r requirements.txt
# xbot 中 /reload-plugins
```

## 🔧 创建 GitLab Token

1. GitLab → Settings → Access Tokens
2. 创建 Personal Access Token：
   - Name: `xbot-cr`
   - Scopes: `api`, `read_user`
3. 复制 token（只显示一次）

## ⚙️ 配置

```json
{
  "channels": {
    "gitlab": {
      "enabled": "true",
      "mode": "poll",
      "gitlab_url": "https://gitlab.com",
      "pat_token": "glpat-xxxxxxxxxx",
      "bot_username": "my-bot",
      "poll_interval": "10",
      "monitored_repos": "",
      "whitelist_repos": "",
      "blacklist_repos": "",
      "whitelist_users": "",
      "blacklist_users": ""
    }
  }
}
```

| 配置项 | 说明 |
|--------|------|
| `gitlab_url` | GitLab 实例地址（默认 gitlab.com） |
| `pat_token` | Personal Access Token |
| `bot_username` | Bot 在 GitLab 上的用户名 |
| `poll_interval` | 轮询间隔（秒） |
| `monitored_repos` | 监控项目列表（逗号分隔，留空=自动发现全部 PAT 可访问项目） |
| `whitelist_repos` | 项目白名单（支持 `group/*` 通配符） |

## 🛠️ Channel Tools

| 工具 | 说明 |
|------|------|
| `get_mr_info` | 获取 MR 基本信息 |
| `get_mr_changes` | 获取 diff + diff_refs（用于行级评论） |
| `post_mr_review` | 发表整体评论 + 行级 discussion |
| `post_mr_note` | 发表普通评论 |

## License

MIT
