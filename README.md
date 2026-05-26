# Linux.Do 知识问答 奖励

AstrBot 插件 —— Linux.Do 社区知识问答，答对奖励邀请码。

## 功能

- **群聊触发**：检测到"邀请码"等关键词，经 LLM 意图识别后主动询问是否需要答题
- **知识库出题**：集成 AstrBot 知识库，自动生成开放式验证题目（知识问答 + 案例分析）
- **LLM 评判**：用户回答后由 LLM 判断是否正确，支持核心观点匹配
- **链接验证**：支持 Cloudflare Worker Browser Run 远程验证邀请链接有效性
- **双通道发送**：QQ 私聊 / 邮件发送邀请码
- **用户投稿**：私聊发送邀请链接，自动验证后存入

## 安装

将插件目录放入 `data/plugins/` 下，重启 AstrBot。

## 配置

在 WebUI 插件配置中设置：

| 配置项 | 说明 | 推荐值 |
|---|---|---|
| `use_worker` | 使用 Cloudflare Worker 验证 | true |
| `verify_api_url` | Worker URL | `https://your-worker.workers.dev` |
| `verify_api_token` | Worker 鉴权 Token | 与 wrangler secret 一致 |
| `kb_names` | 知识库选择 | Linux.Do |
| `judge_model` | 判断模型（意图+判题） | 轻量模型 |
| `question_gen_model` | 出题模型（题库生成） | 强模型 |
| `delivery_method` | 发送方式 | private_message / email |

## 管理指令

| 指令 | 说明 |
|---|---|
| `/存入邀请码 <名称> <链接> <问题> <答案>` | 添加邀请码（永不过期） |
| `/删除邀请码 <ID>` | 删除邀请码 |
| `/邀请码列表` | 列出所有邀请码及状态 |
| `/验证邀请码 <ID>` | 手动验证链接有效性 |
| `/清理过期邀请码` | 清理过期和无效条目 |
| `/刷新题库` | 从知识库重新生成题库 |

## 部署 Cloudflare Worker

```bash
cd worker
npm install
npx wrangler secret put AUTH_TOKEN
npx wrangler deploy
```

`wrangler.toml` 中需配置 Browser Run 绑定：

```toml
[browser]
binding = "BROWSER"
```

## 依赖

- Python: `httpx>=0.24`
- AstrBot >= 4.16
- Cloudflare Worker (可选，用于链接验证)
