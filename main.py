import asyncio
import json
import random
import re
import smtplib
import time
from email.mime.text import MIMEText
from pathlib import Path

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.core.platform.message_type import MessageType
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.utils.session_waiter import SessionController, session_waiter

INVITE_KEYWORDS_REGEX = (
    r"(邀请码|邀请链接|求邀请码|给个邀请码|有邀请码吗"
    r"|怎么注册|注册链接|邀请我|求个码|给个码"
    r"|想要邀请|邀请注册|来个码|求个邀请|求码|给码)"
)

INVITE_URL_PATTERN = re.compile(
    r"https?://[^\s]*/(?:invites?|invite|join|register|signup|referral)/[^\s]+",
    re.IGNORECASE,
)

# 各站点的验证规则
SITE_VERIFY_RULES: list[dict] = [
    {
        "pattern": r"linux\.do",
        "desc": "LINUX DO 邀请链接",
        "valid_text": ["欢迎来到 LINUX DO", "Welcome to LINUX DO"],
        "error_texts": ["无效", "已过期", "invalid", "expired"],
    },
]


class InviteCodePlugin(Star):
    _pw: "async_playwright | None" = None
    _browser: "Browser | None" = None

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config: AstrBotConfig = config if config is not None else {}
        self.data_dir: Path = StarTools.get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_file = self.data_dir / "invite_codes.json"
        self.question_file = self.data_dir / "question_pool.json"
        self.daily_usage_file = self.data_dir / "daily_usage.json"
        self.invite_codes: list[dict] = []
        self.question_pool: list[dict] = []
        self._daily_usage: dict[str, set[str]] = {}
        self._load_data()
        self._load_question_pool()
        self._load_daily_usage()
        self._cleanup_expired()
        self._cleanup_task: asyncio.Task | None = None

    async def initialize(self):
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())

    async def terminate(self):
        if self._cleanup_task:
            self._cleanup_task.cancel()
            self._cleanup_task = None
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None

    # ========== Data Persistence ==========

    def _load_data(self):
        if self.data_file.exists():
            try:
                with open(self.data_file, encoding="utf-8") as f:
                    self.invite_codes = json.load(f)
            except Exception:
                logger.error("加载邀请码数据失败", exc_info=True)
                self.invite_codes = []
        else:
            self.invite_codes = []

    def _save_data(self):
        try:
            with open(self.data_file, "w", encoding="utf-8") as f:
                json.dump(self.invite_codes, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("保存邀请码数据失败", exc_info=True)

    # ========== Question Pool (KB) ==========

    def _load_question_pool(self):
        if self.question_file.exists():
            try:
                with open(self.question_file, encoding="utf-8") as f:
                    self.question_pool = json.load(f)
            except Exception:
                logger.error("加载题库失败", exc_info=True)
                self.question_pool = []
        else:
            self.question_pool = []

    def _save_question_pool(self):
        try:
            with open(self.question_file, "w", encoding="utf-8") as f:
                json.dump(self.question_pool, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("保存题库失败", exc_info=True)

    @staticmethod
    def _today_str() -> str:
        import datetime
        return datetime.date.today().isoformat()

    def _load_daily_usage(self):
        if self.daily_usage_file.exists():
            try:
                with open(self.daily_usage_file, encoding="utf-8") as f:
                    raw = json.load(f)
                today = self._today_str()
                self._daily_usage = {
                    k: set(v) for k, v in raw.items() if k >= today
                }
            except Exception:
                self._daily_usage = {}
        else:
            self._daily_usage = {}

    def _save_daily_usage(self):
        try:
            with open(self.daily_usage_file, "w", encoding="utf-8") as f:
                json.dump(
                    {k: list(v) for k, v in self._daily_usage.items()},
                    f, ensure_ascii=False, indent=2,
                )
        except Exception:
            logger.error("保存每日用量失败", exc_info=True)

    def _check_daily_limit(self, user_id: str) -> bool:
        """检查用户是否超限。返回 True 表示可以继续。"""
        limit = self.config.get("daily_limit", 1)
        if limit <= 0:
            return True
        today = self._today_str()
        used = self._daily_usage.get(today, set())
        return len(used) < limit

    def _record_daily_usage(self, user_id: str):
        today = self._today_str()
        if today not in self._daily_usage:
            self._daily_usage[today] = set()
        self._daily_usage[today].add(user_id)
        self._save_daily_usage()

    def _use_kb(self) -> bool:
        kb_names = self.config.get("kb_names", [])
        return bool(kb_names)

    def _pick_question(self) -> dict | None:
        if not self.question_pool:
            return None
        return random.choice(self.question_pool)

    async def _judge_answer(
        self, event: AstrMessageEvent, question: str, reference_answer: str, user_answer: str,
    ) -> tuple[bool, str]:
        """使用 LLM 判断用户回答是否正确。返回 (is_correct, feedback)"""
        provider = await self._get_judge_provider(event)
        if not provider:
            return user_answer.strip().lower() in reference_answer.lower(), ""

        judge_template = self.config.get("judge_prompt_template", "")
        prompt = (
            f"题目：{question}\n\n"
            f"评判要点：{reference_answer}\n\n"
            f"用户回答：{user_answer}\n\n"
            f"请判断用户回答是否正确。\n"
            f"- 不要求与评判要点逐字一致，意思正确即可\n"
            f"- 核心观点正确即算通过\n"
            f"- 明显错误、偏题、答非所问算不通过\n"
            f"- 回答过于简短或敷衍（如仅\"是\"\"对\"而题目要求解释）算不通过\n\n"
            f"{judge_template}"
        )
        try:
            resp = await provider.text_chat(prompt=prompt)
            text = resp.completion_text.strip()
            if "{" in text:
                text = text[text.index("{"):text.rindex("}") + 1]
            result = json.loads(text)
            correct = result.get("correct", False)
            feedback = result.get("feedback", "")
            return correct, feedback
        except Exception as e:
            logger.warning(f"LLM 判题失败: {e}")
            # 回退：宽松匹配
            fallback = user_answer.strip().lower() in reference_answer.lower()
            return fallback, ""

    def _now_ts(self) -> float:
        return time.time()

    def _is_expired(self, entry: dict) -> bool:
        expires = entry.get("expires_at")
        if not expires:
            return False
        return self._now_ts() > expires

    def _cleanup_expired(self) -> int:
        before = len(self.invite_codes)
        self.invite_codes = [e for e in self.invite_codes if not self._is_expired(e)]
        removed = before - len(self.invite_codes)
        if removed > 0:
            self._save_data()
            logger.info(f"清理了 {removed} 个已过期的邀请码")
        return removed

    async def _periodic_cleanup(self):
        while True:
            try:
                await asyncio.sleep(600)
                self._cleanup_expired()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.warning("定期清理失败", exc_info=True)

    def _pick_random_invite(self) -> dict | None:
        valid = [e for e in self.invite_codes if not self._is_expired(e)]
        if not valid:
            return None
        return random.choice(valid)

    def _get_invite_by_id(self, invite_id: int) -> dict | None:
        for entry in self.invite_codes:
            if entry["id"] == invite_id:
                if self._is_expired(entry):
                    return None
                return entry
        return None

    def _format_expiry(self, entry: dict) -> str:
        expires = entry.get("expires_at")
        if not expires:
            return "永不过期"
        import datetime
        dt = datetime.datetime.fromtimestamp(expires)
        remaining = expires - self._now_ts()
        if remaining <= 0:
            return "已过期"
        hours = int(remaining // 3600)
        minutes = int((remaining % 3600) // 60)
        return f"{dt.strftime('%m-%d %H:%M')}（剩余 {hours}h{minutes}m）"

    @staticmethod
    def _make_private_umo(event: AstrMessageEvent) -> str:
        return f"{event.get_platform_name()}:friend:{event.get_sender_id()}"

    # ========== Browser & Verification ==========

    async def _get_judge_provider(self, event: AstrMessageEvent | None = None):
        """获取判断模型提供商（意图识别 + 答案评判）。"""
        model_id = self.config.get("judge_model", "").strip()
        if model_id:
            prov = await self.context.provider_manager.get_provider_by_id(model_id)
            if prov:
                return prov
            logger.warning(f"判断模型 {model_id} 未找到，回退到默认提供商")
        umo = event.unified_msg_origin if event else None
        return self.context.get_using_provider(umo=umo)

    async def _get_question_gen_provider(self, event: AstrMessageEvent | None = None):
        """获取出题模型提供商（题库生成）。"""
        model_id = self.config.get("question_gen_model", "").strip()
        if model_id:
            prov = await self.context.provider_manager.get_provider_by_id(model_id)
            if prov:
                return prov
            logger.warning(f"出题模型 {model_id} 未找到，回退到默认提供商")
        umo = event.unified_msg_origin if event else None
        return self.context.get_using_provider(umo=umo)

    async def _get_browser(self):
        if self._browser is None:
            from playwright.async_api import async_playwright
            self._pw = await async_playwright().start()
            self._browser = await self._pw.chromium.launch(headless=True)
        return self._browser

    def _detect_site_rule(self, url: str) -> dict | None:
        for rule in SITE_VERIFY_RULES:
            if re.search(rule["pattern"], url):
                return rule
        return None

    def _verify_fail_open(self) -> bool:
        """验证失败/API 不可用时是否放行"""
        return self.config.get("verify_fail_mode", "pass") == "pass"

    async def _verify_invite_link(self, url: str) -> tuple[bool, str]:
        """验证邀请链接是否有效。use_worker 开启时走外部 API；关闭时使用内置 Playwright。"""
        rule = self._detect_site_rule(url)
        if not rule:
            return True, "未匹配已知站点规则，当作有效处理"

        use_worker = self.config.get("use_worker", True)
        if use_worker:
            api_url = self.config.get("verify_api_url", "").strip()
            if api_url:
                return await self._verify_via_api(api_url, url, rule)
            ok = self._verify_fail_open()
            return ok, "Worker API 未配置，跳过验证" if ok else "Worker API 未配置，拒绝存入"

        return await self._verify_via_browser(url, rule)

    async def _verify_via_api(
        self, api_url: str, url: str, rule: dict
    ) -> tuple[bool, str]:
        """通过外部 API 验证链接。"""
        try:
            async with httpx.AsyncClient(timeout=25) as client:
                resp = await client.post(
                    api_url,
                    json={
                        "url": url,
                        "valid_text": rule["valid_text"],
                        "error_texts": rule["error_texts"],
                        "description": rule["desc"],
                        "token": self.config.get("verify_api_token", ""),
                    },
                )
                resp.raise_for_status()
                data = resp.json()
                is_valid = data.get("valid", True)
                msg = data.get("message", "API 返回未知结果")
                logger.debug(f"API 验证结果: url={url[:50]}... valid={is_valid} msg={msg}")
                return is_valid, msg
        except httpx.HTTPStatusError as e:
            logger.warning(f"验证 API 返回错误 {e.response.status_code}: {e}")
            ok = self._verify_fail_open()
            return ok, f"API 返回 {e.response.status_code}，{'当作有效处理' if ok else '拒绝存入'}"
        except Exception as e:
            logger.warning(f"调用验证 API 失败: {e}")
            ok = self._verify_fail_open()
            return ok, f"API 不可用，{'当作有效处理' if ok else '拒绝存入'}: {e}"

    async def _verify_via_browser(
        self, url: str, rule: dict
    ) -> tuple[bool, str]:
        """通过内置 Playwright 浏览器验证链接。检查页面全文内容。"""
        valid_texts = rule["valid_text"]
        if isinstance(valid_texts, str):
            valid_texts = [valid_texts]
        error_texts = rule["error_texts"]
        desc = rule["desc"]

        try:
            browser = await self._get_browser()
            page = await browser.new_page()
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
                title = await page.title()
                status = resp.status if resp else 0
                body_text = await page.evaluate(
                    "() => document.body?.innerText || ''"
                )
                content = title + " " + body_text

                logger.debug(
                    f"验证链接 {url[:50]}... status={status}, title={title}"
                )

                for vt in valid_texts:
                    if vt in content:
                        return True, f"{desc} - 有效"
                for err in error_texts:
                    if err in content:
                        return False, f"{desc} - 无效或已过期（页面包含「{err}」）"
                return False, f"{desc} - 无法确认有效性（页面不含预期内容）"
            finally:
                await page.close()
        except ImportError:
            logger.warning("Playwright 未安装，跳过链接验证")
            ok = self._verify_fail_open()
            return ok, "无法验证（Playwright 未安装）" if ok else "无法验证（Playwright 未安装），拒绝存入"
        except Exception as e:
            logger.warning(f"链接验证异常: {e}")
            ok = self._verify_fail_open()
            return ok, f"验证异常，{'当作有效处理' if ok else '拒绝存入'}: {e}"
            return True, f"验证异常，当作有效处理: {e}"

    async def _generate_question_pool(self, event: AstrMessageEvent | None = None):
        """从知识库检索内容，用 LLM 批量生成验证题目。"""
        kb_names = self.config.get("kb_names", [])
        if not kb_names:
            return 0, "未配置知识库"

        pool_size = self.config.get("question_pool_size", 20)

        # 多轮检索获取多样化内容
        queries = ["社区规则", "注册要求", "行为准则", "等级说明", "常见问题"]
        all_contexts: list[str] = []
        for q in queries:
            try:
                result = await self.context.kb_manager.retrieve(
                    query=q, kb_names=kb_names, top_k_fusion=10, top_m_final=5,
                )
                if result and result.get("context_text"):
                    all_contexts.append(result["context_text"])
            except Exception as e:
                logger.warning(f"知识库检索失败 query={q}: {e}")

        if not all_contexts:
            return 0, "知识库中未检索到内容"

        # 截断控制 token 消耗
        max_context = 4000
        combined = "\n\n---\n\n".join(all_contexts)
        if len(combined) > max_context:
            combined = combined[:max_context] + "\n\n...(content truncated)"

        gen_template = self.config.get("question_gen_prompt_template", "")
        prompt = (
            f"你是一个社区验证题目的出题人。请根据以下社区文档内容，"
            f"生成 {pool_size} 道开放式验证题目，用于验证申请者是否了解该社区。\n\n"
            f"题型要求（不要出选择题）：\n"
            f"1. 知识问答（knowledge）：直接提问社区规则、制度、要求等，用户需用自己的话回答\n"
            f"   例：「社区对新注册用户的发帖限制是什么？」\n"
            f"2. 案例分析（case）：给出一个具体场景，让用户判断是否违规、如何处理\n"
            f"   例：「用户A在多个帖子下回复完全相同的内容推广自己的网站，"
            f"这是否符合社区准则？为什么？」\n\n"
            f"要求：\n"
            f"- 问题基于文档真实内容，不能编造\n"
            f"- 问题覆盖不同方面，不要重复\n"
            f"- reference_answer 写清评判要点，LLM 将据此判断用户回答是否正确\n"
            f"- 知识题和案例题各占一半左右\n\n"
            f"{gen_template}\n\n"
            f"=== 社区文档内容 ===\n{combined}"
        )

        provider = await self._get_question_gen_provider(event)
        if not provider:
            return 0, "无法获取 LLM 提供商"

        try:
            resp = await provider.text_chat(prompt=prompt)
            text = resp.completion_text.strip()
            # 提取 JSON
            if "```" in text:
                text = text.split("```")[1]
                if text.startswith("json"):
                    text = text[4:]
            questions = json.loads(text)
            if not isinstance(questions, list):
                return 0, f"LLM 返回格式异常: {type(questions)}"

            # 分配 ID
            max_id = max((q["id"] for q in self.question_pool), default=0)
            for i, q in enumerate(questions):
                q["id"] = max_id + i + 1
                if "type" not in q:
                    q["type"] = "knowledge"
                # reference_answer 保持原文不 lower，给 LLM 判题用
                q["reference_answer"] = q.get("reference_answer", q.get("answer", ""))

            self.question_pool = questions
            self._save_question_pool()
            logger.info(f"题库已生成，共 {len(questions)} 题")
            return len(questions), ""
        except json.JSONDecodeError as e:
            logger.error(f"LLM 返回的 JSON 解析失败: {text[:500]}")
            return 0, f"JSON 解析失败: {e}"
        except Exception as e:
            logger.error(f"生成题库失败: {e}", exc_info=True)
            return 0, str(e)

    async def _reverify_stored_links(self):
        """定期重新验证已存储的链接，标记无效链接。"""
        for entry in self.invite_codes:
            if entry.get("source") != "user_contributed":
                continue
            if self._is_expired(entry):
                continue
            is_valid, msg = await self._verify_invite_link(entry["code"])
            if entry.get("verified") != is_valid:
                entry["verified"] = is_valid
                entry["verify_msg"] = msg
                logger.info(f"重新验证 ID={entry['id']}: {'有效' if is_valid else '无效'} - {msg}")
        self._save_data()

    # ========== Message Delivery ==========

    async def _send_private_msg(self, event: AstrMessageEvent, code: str, name: str):
        private_umo = self._make_private_umo(event)
        chain = MessageChain().message(
            f"你好，这是你要的邀请码【{name}】：\n{code}\n\n请尽快使用。"
        )
        await self.context.send_message(private_umo, chain)
        logger.info(f"已向 {event.get_sender_id()} 私发邀请码【{name}】")

    async def _send_email(self, to_email: str, code: str, name: str):
        email_cfg = self.config.get("email_config", {})
        smtp_host = email_cfg.get("smtp_host", "")
        smtp_port = email_cfg.get("smtp_port", 587)
        user = email_cfg.get("user", "")
        password = email_cfg.get("password", "")
        from_name = email_cfg.get("from_name", "AstrBot")

        if not smtp_host or not user or not password:
            raise ValueError("邮箱配置不完整，请联系管理员完善 SMTP 配置")

        msg = MIMEText(
            f"你好！\n\n这是你要的邀请码【{name}】：\n{code}\n\n请尽快使用。\n\n--- AstrBot",
            "plain",
            "utf-8",
        )
        msg["Subject"] = f"邀请码【{name}】"
        msg["From"] = f"{from_name} <{user}>"
        msg["To"] = to_email

        def _sync_send():
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                server.starttls()
                server.login(user, password)
                server.send_message(msg)

        await asyncio.to_thread(_sync_send)
        logger.info(f"已向 {to_email} 发送邀请码【{name}】")

    # ========== Private Chat: Auto-Detect Invite URL ==========

    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def on_private_url_detect(self, event: AstrMessageEvent):
        """检测私聊中的邀请链接，验证后自动存入。"""
        text = event.message_str.strip()
        match = INVITE_URL_PATTERN.search(text)
        if not match:
            return

        url = match.group(0)

        # 去重
        for entry in self.invite_codes:
            if entry["code"] == url and not self._is_expired(entry):
                verified = "已验证有效" if entry.get("verified") else "待验证"
                yield event.plain_result(
                    f"这个邀请链接已经存在（ID={entry['id']}，{verified}）。\n"
                    f"过期时间：{self._format_expiry(entry)}"
                )
                event.stop_event()
                return

        enable_verify = self.config.get("enable_verify", True)
        if enable_verify:
            yield event.plain_result("正在验证邀请链接有效性，请稍候...")
            is_valid, verify_msg = await self._verify_invite_link(url)
            if not is_valid:
                yield event.plain_result(f"此邀请链接无效：{verify_msg}\n请确认链接正确后重新发送。")
                event.stop_event()
                return
        else:
            verify_msg = "未验证"

        new_id = max((e["id"] for e in self.invite_codes), default=0) + 1
        expire_hours = self.config.get("expire_hours", 24)
        expires_at = self._now_ts() + expire_hours * 3600 if expire_hours > 0 else None
        sender_name = event.get_sender_name()
        sender_id = event.get_sender_id()

        entry = {
            "id": new_id,
            "name": f"用户投稿-{sender_name}",
            "code": url,
            "question": self.config.get("default_question", "请回答：这个社区的简称是什么？"),
            "answer": self.config.get("default_answer", "L站").strip().lower(),
            "expires_at": expires_at,
            "source": "user_contributed",
            "contributor": sender_id,
            "verified": is_valid if enable_verify else None,
            "verify_msg": verify_msg,
        }
        self.invite_codes.append(entry)
        self._save_data()
        logger.info(f"用户 {sender_id} 贡献邀请链接 ID={new_id}，验证结果: {verify_msg}")

        yield event.plain_result(
            f"邀请链接已验证有效（{verify_msg}），已自动存入！\n"
            f"过期时间：{self._format_expiry(entry)}\n"
            + (
                "已启用知识库出题，群友获取时会从题库随机抽题。"
                if self._use_kb()
                else f"其他群友获取时需要回答：{entry['question']}"
            )
        )

    # ========== Admin Commands ==========

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("存入邀请码")
    async def add_invite_code(
        self, event: AstrMessageEvent, name: str, code: str, question: str, answer: str
    ):
        """存入邀请码（永不过期）。用法: /存入邀请码 <名称> <链接> <问题> <答案>"""
        new_id = max((e["id"] for e in self.invite_codes), default=0) + 1
        entry = {
            "id": new_id,
            "name": name.strip(),
            "code": code.strip(),
            "question": question.strip(),
            "answer": answer.strip().lower(),
            "expires_at": None,
            "source": "admin",
            "contributor": event.get_sender_id(),
            "verified": None,
            "verify_msg": "",
        }
        self.invite_codes.append(entry)
        self._save_data()
        yield event.plain_result(
            f"邀请码已存入（永不过期）！\nID: {new_id}\n名称: {name}\n问题: {question}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("验证邀请码")
    async def verify_invite_cmd(self, event: AstrMessageEvent, id: int):
        """手动验证指定邀请码是否有效。用法: /验证邀请码 <ID>"""
        entry = self._get_invite_by_id(id)
        if not entry:
            # 不过滤过期，允许查看所有
            for e in self.invite_codes:
                if e["id"] == id:
                    entry = e
                    break
            else:
                yield event.plain_result(f"未找到 ID={id} 的邀请码。")
                return

        if self.config.get("enable_verify", True):
            yield event.plain_result(f"正在验证 ID={id} 的邀请链接...")
            is_valid, msg = await self._verify_invite_link(entry["code"])
            entry["verified"] = is_valid
            entry["verify_msg"] = msg
            self._save_data()
            status = "有效" if is_valid else "无效"
            yield event.plain_result(
                f"验证完成！\nID={id} | {entry['name']}\n"
                f"结果: {status}\n详情: {msg}"
            )
        else:
            yield event.plain_result("链接验证功能未启用，请在配置中开启 enable_verify。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("删除邀请码")
    async def del_invite_code(self, event: AstrMessageEvent, id: int):
        """删除一个邀请码。用法: /删除邀请码 <ID>"""
        for i, entry in enumerate(self.invite_codes):
            if entry["id"] == id:
                self.invite_codes.pop(i)
                self._save_data()
                yield event.plain_result(f"邀请码 ID={id}（{entry['name']}）已删除。")
                return
        yield event.plain_result(f"未找到 ID={id} 的邀请码。")

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("邀请码列表")
    async def list_invite_codes(self, event: AstrMessageEvent):
        """列出所有邀请码及过期/验证状态。群聊中自动隐藏邀请链接。"""
        self._cleanup_expired()
        if not self.invite_codes:
            yield event.plain_result("暂无可用的邀请码。")
            return
        is_group = event.get_message_type() == MessageType.GROUP_MESSAGE
        lines = []
        for e in self.invite_codes:
            expired = "已过期" if self._is_expired(e) else self._format_expiry(e)
            verified = e.get("verified")
            if verified is True:
                v_status = "有效"
            elif verified is False:
                v_status = "无效"
            else:
                v_status = "未验证"
            code = e["code"]
            if is_group and len(code) > 20:
                code = code[:10] + "****" + code[-6:]
            lines.append(
                f"ID={e['id']} | {e['name']} | {v_status} | {expired}\n"
                f"    {code}"
            )
        yield event.plain_result("邀请码列表：\n" + "\n".join(lines))

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("清理过期邀请码")
    async def cleanup_expired_cmd(self, event: AstrMessageEvent):
        """手动清理所有已过期或验证无效的邀请码。"""
        expired_count = self._cleanup_expired()
        invalid_count = 0
        if self.config.get("enable_verify", True):
            before = len(self.invite_codes)
            self.invite_codes = [e for e in self.invite_codes if e.get("verified") is not False]
            invalid_count = before - len(self.invite_codes)
            self._save_data()
        total = expired_count + invalid_count
        yield event.plain_result(
            f"已清理 {expired_count} 个过期 + {invalid_count} 个无效邀请码，"
            f"共 {total} 个，剩余 {len(self.invite_codes)} 个。"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("刷新题库")
    async def refresh_question_pool_cmd(self, event: AstrMessageEvent):
        """从知识库重新生成题库。"""
        if not self._use_kb():
            yield event.plain_result("知识库模式未启用，请在配置中选择 kb_names。")
            return
        yield event.plain_result("正在从知识库生成题目，请稍候...")
        count, err = await self._generate_question_pool(event)
        if err:
            yield event.plain_result(f"生成失败: {err}")
        else:
            yield event.plain_result(f"题库已刷新，共 {count} 题。")

    # ========== Group Keyword Detection ==========

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    @filter.regex(INVITE_KEYWORDS_REGEX)
    async def on_group_ask_invite(self, event: AstrMessageEvent):
        """检测到群成员提到邀请码相关词汇，交 LLM 判断意图后再决定是否启动问答。"""
        if event.get_sender_id() == event.get_self_id():
            return
        msg = event.message_str.strip()
        if msg.startswith(("/", "!", "#", "！")) or msg.startswith("邀请码"):
            return

        # LLM 意图判断：是真心在求邀请码，还是只是讨论交流
        try:
            provider = await self._get_judge_provider(event)
            if provider:
                resp = await provider.text_chat(
                    prompt=(
                        f'群聊中用户说了：「{msg}」\n'
                        f'请判断：该用户是真心在索要/求邀请码或邀请链接吗？\n'
                        f'只回复一个字：是 或 否。如果是讨论邀请码怎么用、邀请码是什么、'
                        f'或者只是在聊天中提到邀请码，回复"否"。'
                    ),
                )
                intent = resp.completion_text.strip()
                logger.debug(f"邀请码意图判断: msg={msg[:50]} intent={intent}")
                if "否" in intent or "不是" in intent:
                    return
        except Exception as e:
            logger.debug(f"意图判断失败，回退到直接触发: {e}")

        if not self._check_daily_limit(event.get_sender_id()):
            yield event.plain_result("你今天已经获取过邀请码了，明天再来吧。")
            event.stop_event()
            return

        invite = self._pick_random_invite()
        if not invite:
            yield event.plain_result("暂无可用的邀请码。你可以在私聊中向机器人发送邀请链接来贡献。")
            event.stop_event()
            return

        use_kb = self._use_kb()
        kb_question = self._pick_question() if use_kb else None
        if use_kb and not kb_question:
            # 题库为空，尝试自动生成
            count, _ = await self._generate_question_pool(event)
            if count > 0:
                kb_question = self._pick_question()

        timeout = self.config.get("session_timeout", 120)
        retry_limit = self.config.get("allow_retry", 3)
        delivery = self.config.get("delivery_method", "private_message")
        attempts = 0
        confirm_phase = True

        expiry_hint = (
            f"（{self._format_expiry(invite)}）" if invite.get("expires_at") else ""
        )
        if kb_question:
            question_text = kb_question["question"]
            reference_answer = kb_question.get("reference_answer", "")
        else:
            question_text = invite.get("question", self.config.get("default_question", ""))
            correct_answer = invite.get("answer", self.config.get("default_answer", "L站")).strip().lower()

        confirm_prompt = (
            "检测到你可能需要 Linux.Do 邀请码，"
            "是否要进行答题获取？\n回复「是」开始答题，回复「退出」取消。"
        )
        yield event.plain_result(confirm_prompt)

        sender_id = event.get_sender_id()

        try:

            @session_waiter(timeout=timeout)
            async def waiter(controller: SessionController, e: AstrMessageEvent):
                nonlocal attempts, confirm_phase, invite, question_text, kb_question
                nonlocal reference_answer, correct_answer, use_kb, expiry_hint

                # 只响应发起者，忽略其他人
                if e.get_sender_id() != sender_id:
                    controller.keep(timeout=timeout, reset_timeout=True)
                    return

                text = e.message_str.strip()
                if not text:
                    controller.keep(timeout=timeout, reset_timeout=True)
                    return

                if text == "退出":
                    await e.send(e.plain_result("已取消。"))
                    controller.stop()
                    return

                # Phase 1: 确认是否要答题
                if confirm_phase:
                    if text in ("是", "要", "好", "yes", "y", "ok", "嗯", "对", "可以"):
                        confirm_phase = False
                        question_prompt = (
                            f"【{invite['name']}】{expiry_hint}\n\n"
                            f"{question_text}\n\n直接回复答案，发送「退出」可取消。"
                        )
                        await e.send(e.plain_result(question_prompt))
                        controller.keep(timeout=timeout, reset_timeout=True)
                        return
                    else:
                        # 不回复，静默等待，超时自动取消
                        controller.keep(timeout=timeout, reset_timeout=True)
                        return

                attempts += 1

                # KB 模式：LLM 判题；非 KB 模式：字符串匹配
                passed = False
                judge_feedback = ""
                if use_kb:
                    await e.send(e.plain_result("正在评判你的回答..."))
                    passed, judge_feedback = await self._judge_answer(
                        e, question_text, reference_answer, text,
                    )
                    if judge_feedback:
                        await e.send(e.plain_result(judge_feedback))
                else:
                    passed = text.lower() == correct_answer

                if passed:
                    if self._is_expired(invite):
                        await e.send(e.plain_result("抱歉，这个邀请码刚刚过期了。"))
                        controller.stop()
                        return

                    # 发放前二次验证
                    enable_verify = self.config.get("enable_verify", True)
                    if enable_verify:
                        await e.send(e.plain_result("回答正确，正在验证链接有效性..."))
                        is_valid, verify_msg = await self._verify_invite_link(invite["code"])
                        if not is_valid:
                            invite["verified"] = False
                            invite["verify_msg"] = verify_msg
                            self._save_data()
                            new_invite = self._pick_random_invite()
                            if new_invite and new_invite["id"] != invite["id"]:
                                invite = new_invite
                                attempts = 0
                                confirm_phase = True
                                # 换题
                                if use_kb:
                                    kb_question = self._pick_question()
                                if kb_question:
                                    question_text = kb_question["question"]
                                    reference_answer = kb_question.get("reference_answer", "")
                                else:
                                    question_text = invite.get("question", self.config.get("default_question", ""))
                                    correct_answer = invite.get("answer", self.config.get("default_answer", "L站")).strip().lower()
                                expiry_hint = (
                                    f"（{self._format_expiry(invite)}）"
                                    if invite.get("expires_at") else ""
                                )
                                await e.send(e.plain_result(
                                    f"该邀请链接已失效（{verify_msg}），为你更换另一个。\n\n"
                                    "检测到你可能需要 Linux.Do 邀请码，"
                                    "是否要答题获取？回复「是」开始，回复「退出」取消。"
                                ))
                                controller.keep(timeout=timeout, reset_timeout=True)
                                return
                            await e.send(e.plain_result(
                                f"该邀请链接已失效（{verify_msg}），且暂无其他可用邀请码。"
                            ))
                            controller.stop()
                            return

                    if delivery == "email":
                        qq_email = f"{e.get_sender_id()}@qq.com"
                        try:
                            await self._send_email(qq_email, invite["code"], invite["name"])
                            self._record_daily_usage(e.get_sender_id())
                            await e.send(e.plain_result(f"回答正确！邀请码已发送到 {qq_email}，请查收。"))
                        except Exception as exc:
                            logger.error(f"邮件发送失败: {exc}")
                            await e.send(e.plain_result(f"邮件发送失败: {exc}"))
                        controller.stop()
                        return

                    await e.send(e.plain_result("回答正确，正在私发邀请码，请查看私聊。"))
                    try:
                        await self._send_private_msg(e, invite["code"], invite["name"])
                        self._record_daily_usage(e.get_sender_id())
                    except Exception:
                        await e.send(e.plain_result(
                            "私发失败，请确认已添加机器人为好友，或联系管理员。"
                        ))
                    controller.stop()
                    return

                remaining = retry_limit - attempts if retry_limit > 0 else None
                if retry_limit > 0 and remaining <= 0:
                    if use_kb:
                        await e.send(e.plain_result(
                            "已达最大重试次数。请重新发送关键词发起新请求。"
                        ))
                    else:
                        await e.send(e.plain_result(
                            f"回答错误，已达最大重试次数。正确答案是「{correct_answer}」。"
                            f"请重新发送关键词发起新请求。"
                        ))
                    controller.stop()
                    return

                hint = f"还可重试 {remaining} 次" if remaining else "请再试一次"
                await e.send(e.plain_result(f"回答错误，{hint}。发送「退出」可取消。"))

            await waiter(event)
        except TimeoutError:
            if use_kb:
                yield event.plain_result("验证超时，请重新发送关键词发起新请求。")
            else:
                yield event.plain_result(f"验证超时。正确答案是「{correct_answer}」。")
        except Exception as exc:
            logger.error(f"邀请码验证流程异常: {exc}", exc_info=True)
            yield event.plain_result("验证流程出错，请稍后再试。")
        finally:
            event.stop_event()

    # ========== LLM Tool: Get Invite Question ==========

    @filter.llm_tool(name="get_invite_question")
    async def llm_get_invite_question(self, event: AstrMessageEvent):
        """获取一个随机邀请码和对应的验证问题。当用户想要注册链接或邀请码时调用。
        返回的 expected_answer 需要传给 check_invite_answer 用于验证。"""
        self._cleanup_expired()
        invite = self._pick_random_invite()
        if not invite:
            return "暂无可用的邀请码。引导用户私聊机器人发送邀请链接。"

        if self._use_kb():
            kb_question = self._pick_question()
            if not kb_question:
                return "题库为空，请联系管理员使用「/刷新题库」生成题目。"
            question = kb_question["question"]
            reference = kb_question.get("reference_answer", "")
            return json.dumps(
                {
                    "invite_id": invite["id"],
                    "name": invite["name"],
                    "question": question,
                    "reference_answer": reference,
                    "kb_mode": True,
                    "expires_at": invite.get("expires_at"),
                },
                ensure_ascii=False,
            )
        else:
            question = invite.get("question", self.config.get("default_question", ""))
            expected_answer = invite.get("answer", self.config.get("default_answer", "L站")).strip().lower()
            return json.dumps(
                {
                    "invite_id": invite["id"],
                    "name": invite["name"],
                    "question": question,
                    "expected_answer": expected_answer,
                    "kb_mode": False,
                    "expires_at": invite.get("expires_at"),
                },
                ensure_ascii=False,
            )

    # ========== LLM Tool: Check Answer & Deliver ==========

    @filter.llm_tool(name="check_invite_answer")
    async def llm_check_invite_answer(
        self, event: AstrMessageEvent, invite_id: int, answer: str,
        expected_answer: str = "", kb_mode: bool = False, question: str = "",
    ):
        """验证用户对邀请码问题的回答，正确则私发邀请码。

        Args:
            invite_id(int): 邀请码ID
            answer(string): 用户给出的回答
            expected_answer(string): 期望答案（kb_mode=False时用于字符串匹配）
            kb_mode(bool): 是否知识库出题模式
            question(string): 题目原文（kb_mode=True时用于LLM判题）
        """
        invite = self._get_invite_by_id(invite_id)
        if not invite:
            return "错误：该邀请码不存在或已过期，请重新调用 get_invite_question。"

        if kb_mode and question:
            passed, feedback = await self._judge_answer(
                event, question, expected_answer, answer,
            )
            if not passed:
                return feedback or "回答错误，请引导用户再试一次。"
        elif answer.strip().lower() != expected_answer.strip().lower():
            return "回答错误，请引导用户再试一次。"

        if self._is_expired(invite):
            return "该邀请码已过期，请重新调用 get_invite_question。"

        # 发放前二次验证
        enable_verify = self.config.get("enable_verify", True)
        if enable_verify:
            is_valid, verify_msg = await self._verify_invite_link(invite["code"])
            if not is_valid:
                invite["verified"] = False
                invite["verify_msg"] = verify_msg
                self._save_data()
                new_invite = self._pick_random_invite()
                if new_invite and new_invite["id"] != invite_id:
                    return json.dumps(
                        {
                            "action": "retry",
                            "id": new_invite["id"],
                            "name": new_invite["name"],
                            "question": new_invite["question"],
                            "message": f"该链接已失效（{verify_msg}），已更换另一个，请重新提问。",
                        },
                        ensure_ascii=False,
                    )
                return "该链接已失效，且暂无其他可用邀请码。"

        delivery = self.config.get("delivery_method", "private_message")
        if delivery == "email":
            return (
                "回答正确，但当前配置为邮箱发送。请引导用户提供邮箱地址，"
                "或由管理员通过私聊手动发送。"
            )

        try:
            await self._send_private_msg(event, invite["code"], invite["name"])
        except Exception as exc:
            logger.error(f"LLM tool 私发邀请码失败: {exc}")
            return f"私发失败: {exc}。请告知用户确认已添加机器人为好友。"

        return "邀请码已通过私聊发送给用户，请告知用户查看私聊。"
