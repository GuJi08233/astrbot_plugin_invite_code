from __future__ import annotations

import asyncio
import datetime
import json
import os
import random
import re
import secrets
import smtplib
import tempfile
import time
from dataclasses import dataclass
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from playwright.async_api import Browser, async_playwright

import httpx

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.utils.session_waiter import SessionController, session_waiter

INVITE_KEYWORDS_REGEX = (
    r"(邀请码|邀请链接|邀请注册|社区邀请|论坛邀请|注册码"
    r"|求邀请码|求个邀请|求邀请|求码|求个码|求链接|求个链接"
    r"|给个邀请码|给个码|给码|给个邀请|来个码|来个邀请"
    r"|有邀请码吗|有码吗|还有码吗|有没有码|有没有邀请"
    r"|想要邀请|想注册|怎么注册|注册链接|邀请我"
    r"|L\s*站|L站|Linux\s*Do|LinuxDo"
    r"|佬友|有佬吗|求个佬|来个佬|有没有佬)"
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


@dataclass
class ChallengeSession:
    """Mutable state for a group invite challenge flow."""
    attempts: int = 0
    confirm_phase: bool = True
    invite: dict | None = None
    question_text: str = ""
    reference_answer: str = ""
    correct_answer: str = ""
    use_kb: bool = False
    kb_question: dict | None = None
    expiry_hint: str = ""



class InviteCodePlugin(Star):
    _pw: async_playwright | None = None  # noqa: F821
    _browser: Browser | None = None  # noqa: F821

    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        self.config: AstrBotConfig = config if config is not None else {}
        self.data_dir: Path = StarTools.get_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.data_file = self.data_dir / "invite_codes.json"
        self.question_file = self.data_dir / "question_pool.json"
        self.weekly_usage_file = self.data_dir / "weekly_usage.json"
        self.invite_codes: list[dict] = []
        self.question_pool: list[dict] = []
        self._weekly_usage: dict[str, dict[str, int]] = {}
        # LLM-tool 出题会话:challenge_token -> {invite_id, question, reference_answer,
        # kb_mode, user_id, expires_at}。避免把正确答案/kb_mode 暴露给 LLM 入参往返。
        self._pending_challenges: dict[str, dict] = {}
        self._browser_lock: asyncio.Lock = asyncio.Lock()
        self._locked_invites: set[int] = set()  # invite IDs currently being challenged
        self._question_pool_refilling: bool = False
        self._load_data()
        self._load_question_pool()
        self._load_weekly_usage()
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
        """Atomically write invite codes to disk via temp file."""
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                suffix=".json", dir=self.data_dir,
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(self.invite_codes, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self.data_file)
            except Exception:
                os.unlink(tmp_path)
                raise
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
        """Atomically write question pool to disk via temp file."""
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                suffix=".json", dir=self.data_dir,
            )
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    json.dump(self.question_pool, f, ensure_ascii=False, indent=2)
                os.replace(tmp_path, self.question_file)
            except Exception:
                os.unlink(tmp_path)
                raise
        except Exception:
            logger.error("保存题库失败", exc_info=True)

    @staticmethod
    def _week_str() -> str:
        """返回当前 ISO 周标识，如 '2026-W23'。"""
        today = datetime.date.today()
        iso = today.isocalendar()
        return f"{iso[0]}-W{iso[1]:02d}"

    def _load_weekly_usage(self):
        """加载每周用量。自动迁移旧文件 daily_usage.json → weekly_usage.json。"""
        # 迁移旧文件
        old_file = self.data_dir / "daily_usage.json"
        if old_file.exists() and not self.weekly_usage_file.exists():
            try:
                old_file.rename(self.weekly_usage_file)
                logger.info("已迁移 daily_usage.json → weekly_usage.json")
            except Exception:
                pass

        if self.weekly_usage_file.exists():
            try:
                with open(self.weekly_usage_file, encoding="utf-8") as f:
                    raw = json.load(f)
                current_week = self._week_str()
                normalized: dict[str, dict[str, int]] = {}
                for key, val in raw.items():
                    # 兼容旧格式: 日期 key 归入对应 ISO 周
                    if len(key) == 10 and key[4] == "-":
                        # "2026-06-04" → "2026-W23"
                        try:
                            d = datetime.date.fromisoformat(key)
                            iso = d.isocalendar()
                            week_key = f"{iso[0]}-W{iso[1]:02d}"
                        except ValueError:
                            continue
                    else:
                        week_key = key
                    if week_key < current_week:
                        continue
                    if isinstance(val, list):
                        normalized.setdefault(week_key, {})
                        for uid in val:
                            normalized[week_key][uid] = normalized[week_key].get(uid, 0) + 1
                    elif isinstance(val, dict):
                        normalized.setdefault(week_key, {})
                        for k, v in val.items():
                            normalized[week_key][k] = normalized[week_key].get(k, 0) + int(v)
                self._weekly_usage = normalized
            except Exception:
                self._weekly_usage = {}
        else:
            self._weekly_usage = {}

    def _save_weekly_usage(self):
        try:
            with open(self.weekly_usage_file, "w", encoding="utf-8") as f:
                json.dump(self._weekly_usage, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.error("保存每周用量失败", exc_info=True)

    def _check_weekly_limit(self, user_id: str) -> bool:
        """检查用户本周是否仍可领取(per-user)。True 表示可继续。"""
        limit = self.config.get("weekly_limit", self.config.get("daily_limit", 3))
        if limit <= 0:
            return True
        week = self._week_str()
        used = self._weekly_usage.get(week, {})
        return used.get(user_id, 0) < limit

    def _record_weekly_usage(self, user_id: str):
        week = self._week_str()
        if week not in self._weekly_usage:
            self._weekly_usage[week] = {}
        self._weekly_usage[week][user_id] = self._weekly_usage[week].get(user_id, 0) + 1
        self._save_weekly_usage()

    async def _check_qq_level(self, event: AstrMessageEvent) -> bool:
        """检查用户 QQ 账号等级是否满足要求。True 表示通过。仅 aiocqhttp 平台生效。"""
        min_level = self.config.get("min_qq_level", 0)
        if min_level <= 0:
            return True
        if event.get_platform_name() != "aiocqhttp":
            return True
        try:
            bot = getattr(event, "bot", None)
            if not bot:
                return True
            info = await bot.call_action(
                action="get_stranger_info",
                user_id=int(event.get_sender_id()),
                no_cache=False,
            )
            user_level = int(info.get("level", 0))
            if user_level < min_level:
                return False
            return True
        except Exception as e:
            logger.warning(f"获取 QQ 等级失败，跳过等级检查: {e}")
            return True

    async def _check_group_level(self, event: AstrMessageEvent) -> bool:
        """检查用户群活跃等级是否满足要求。True 表示通过。仅 aiocqhttp 群聊生效。"""
        min_group_level = self.config.get("min_group_level", "")
        if not min_group_level:
            return True
        if event.get_platform_name() != "aiocqhttp":
            return True
        if not event.get_group_id():
            return True  # 私聊跳过群等级检查

        GROUP_LEVEL_ORDER = {
            "": 0, "潜水": 1, "冒泡": 2, "吐槽": 3,
            "活跃": 4, "话唠": 5, "龙王": 6,
        }
        required_rank = GROUP_LEVEL_ORDER.get(min_group_level, 0)
        if required_rank <= 0:
            return True

        try:
            bot = getattr(event, "bot", None)
            if not bot:
                return True
            info = await bot.call_action(
                action="get_group_member_info",
                group_id=int(event.get_group_id()),
                user_id=int(event.get_sender_id()),
                no_cache=False,
            )
            user_level_str = info.get("level", "")
            user_rank = GROUP_LEVEL_ORDER.get(user_level_str, 0)
            return user_rank >= required_rank
        except Exception as e:
            logger.warning(f"获取群活跃等级失败，跳过群等级检查: {e}")
            return True

    def _use_kb(self) -> bool:
        kb_names = self.config.get("kb_names", [])
        return bool(kb_names)

    async def _trigger_question_pool_refill(self, event: AstrMessageEvent | None = None):
        """Auto-refill question pool in background if below minimum."""
        if self._question_pool_refilling:
            return
        min_size = self.config.get("question_pool_min_size", 5)
        if len(self.question_pool) >= min_size:
            return
        self._question_pool_refilling = True
        try:
            count, err = await self._generate_question_pool(event)
            if err:
                logger.warning(f"题库自动补充失败: {err}")
            elif count > 0:
                logger.info(f"题库自动补充完成，共 {count} 题")
        finally:
            self._question_pool_refilling = False

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
            f'- 回答过于简短或敷衍（如仅"是""对"而题目要求解释）算不通过\n\n'
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

    def _cleanup_invalid(self) -> int:
        """清理已被标记 verified=False 的邀请码。"""
        before = len(self.invite_codes)
        self.invite_codes = [e for e in self.invite_codes if e.get("verified") is not False]
        removed = before - len(self.invite_codes)
        if removed > 0:
            self._save_data()
            logger.info(f"清理了 {removed} 个验证失败的邀请码")
        return removed

    async def _periodic_cleanup(self):
        while True:
            try:
                await asyncio.sleep(600)
                self._cleanup_expired()
                if self.config.get("enable_verify", True):
                    self._cleanup_invalid()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.warning("定期清理失败", exc_info=True)

    def _pick_random_invite(self) -> dict | None:
        valid = [
            e for e in self.invite_codes
            if not self._is_expired(e)
            and e.get("verified") is not False
            and e["id"] not in self._locked_invites
        ]
        if not valid:
            return None
        return random.choice(valid)

    def _consume_invite(self, invite_id: int) -> bool:
        """发放后从池中移除,避免一次性邀请链接被反复发放。"""
        for i, entry in enumerate(self.invite_codes):
            if entry["id"] == invite_id:
                self.invite_codes.pop(i)
                self._save_data()
                logger.info(f"邀请码 ID={invite_id} 已发放,从池中移除")
                return True
        return False

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

    async def _resolve_email(self, event: AstrMessageEvent) -> str | None:
        """根据平台推断收件邮箱。
        - aiocqhttp(QQ): 默认 {sender_id}@qq.com
        - 其他平台: 无法可靠推断,返回 None,由调用方处理。
        """
        if event.get_platform_name() == "aiocqhttp":
            return f"{event.get_sender_id()}@qq.com"
        return None

    # ========== Browser & Verification ==========

    async def _get_intent_provider(self, event: AstrMessageEvent | None = None):
        """Get intent detection provider (primary then fallback)."""
        for key in ("intent_model", "intent_fallback_model"):
            model_id = self.config.get(key, "").strip()
            if model_id:
                prov = await self.context.provider_manager.get_provider_by_id(model_id)
                if prov:
                    return prov
        umo = event.unified_msg_origin if event else None
        return self.context.get_using_provider(umo=umo)

    async def _get_judge_provider(self, event: AstrMessageEvent | None = None):
        """Get answer judge provider (primary then fallback)."""
        for key in ("judge_model", "judge_fallback_model"):
            model_id = self.config.get(key, "").strip()
            if model_id:
                prov = await self.context.provider_manager.get_provider_by_id(model_id)
                if prov:
                    return prov
        umo = event.unified_msg_origin if event else None
        return self.context.get_using_provider(umo=umo)

    async def _get_question_gen_provider(self, event: AstrMessageEvent | None = None):
        """Get question generation provider (primary then fallback)."""
        for key in ("question_gen_model", "question_gen_fallback_model"):
            model_id = self.config.get(key, "").strip()
            if model_id:
                prov = await self.context.provider_manager.get_provider_by_id(model_id)
                if prov:
                    return prov
        umo = event.unified_msg_origin if event else None
        return self.context.get_using_provider(umo=umo)

    async def _get_browser(self):
        if self._browser is not None:
            return self._browser
        async with self._browser_lock:
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
        """验证邀请链接是否有效。use_worker 开启时走外部 API;关闭时使用内置 Playwright。"""
        rule = self._detect_site_rule(url)
        if not rule:
            # 未匹配已知站点规则:按 verify_fail_mode 决定,避免任意 URL 被静默放行
            ok = self._verify_fail_open()
            return ok, (
                "未匹配已知站点规则,当作有效处理"
                if ok
                else "未匹配已知站点规则,拒绝存入(verify_fail_mode=reject)"
            )

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

            # 整体替换题库,ID 从 1 起重新编号
            for i, q in enumerate(questions, start=1):
                q["id"] = i
                if "type" not in q:
                    q["type"] = "knowledge"
                # reference_answer 保持原文不 lower,给 LLM 判题用
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

        note = self.config.get("email_note", "").strip()
        body = f"你好！\n\n这是你要的邀请码【{name}】：\n{code}\n\n请尽快使用。\n\n发送时间：{time.strftime('%Y-%m-%d %H:%M:%S')}"
        if note:
            body += f"\n\n{note}"
        body += "\n--- AstrBot"
        msg = MIMEText(body,
            "plain",
            "utf-8",
        )
        msg["Subject"] = f"邀请码【{name}】"
        msg["From"] = formataddr((from_name, user))
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
    async def add_invite_code(self, event: AstrMessageEvent):
        """多步交互存入邀请码(永不过期)。命令解析按空格切分,问题/答案常含空格,
        因此改为依次提问:名称 → 链接 → 问题 → 答案。发送「取消」退出。"""
        timeout = self.config.get("session_timeout", 120)
        admin_id = event.get_sender_id()
        steps = [
            ("name", "请输入邀请码名称(发送「取消」退出):"),
            ("code", "请输入邀请链接:"),
            ("question", "请输入验证问题(可包含空格):"),
            ("answer", "请输入正确答案(可包含空格):"),
        ]
        collected: dict[str, str] = {}

        yield event.plain_result(steps[0][1])

        try:
            @session_waiter(timeout=timeout)
            async def waiter(controller: SessionController, e: AstrMessageEvent):
                if e.get_sender_id() != admin_id:
                    controller.keep(timeout=timeout, reset_timeout=True)
                    return
                text = e.message_str.strip()
                if not text:
                    controller.keep(timeout=timeout, reset_timeout=True)
                    return
                if text == "取消":
                    await e.send(e.plain_result("已取消存入。"))
                    controller.stop()
                    return

                idx = len(collected)
                key, _ = steps[idx]
                collected[key] = text

                if idx + 1 < len(steps):
                    await e.send(e.plain_result(steps[idx + 1][1]))
                    controller.keep(timeout=timeout, reset_timeout=True)
                    return
                controller.stop()

            await waiter(event)
        except TimeoutError:
            yield event.plain_result("存入超时,请重新发起。")
            return
        except Exception as exc:
            logger.error(f"/存入邀请码 交互异常: {exc}", exc_info=True)
            yield event.plain_result("存入流程出错。")
            return

        if len(collected) < len(steps):
            return

        new_id = max((e["id"] for e in self.invite_codes), default=0) + 1
        entry = {
            "id": new_id,
            "name": collected["name"],
            "code": collected["code"],
            "question": collected["question"],
            "answer": collected["answer"].strip().lower(),
            "expires_at": None,
            "source": "admin",
            "contributor": event.get_sender_id(),
            "verified": None,
            "verify_msg": "",
        }
        self.invite_codes.append(entry)
        self._save_data()
        yield event.plain_result(
            f"邀请码已存入(永不过期)!\n"
            f"ID: {new_id}\n名称: {entry['name']}\n问题: {entry['question']}"
        )

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("验证")
    async def verify_url_cmd(self, event: AstrMessageEvent, url_or_code: str):
        """验证邀请链接或邀请码是否有效。用法: /验证 <链接|10位邀请码>"""
        url = url_or_code.strip()
        # Auto-construct URL from 10-char invite code
        if re.match(r"^[A-Za-z0-9]{8,12}$", url):
            url = f"https://linux.do/invites/{url}"
        elif not url.startswith("http"):
            yield event.plain_result("请提供有效的邀请链接或 8~12 位邀请码。\n用法: /验证 https://linux.do/invites/xxx 或 /验证 abc123def4")
            return

        if not self.config.get("enable_verify", True):
            yield event.plain_result("链接验证功能未启用，请在配置中开启 enable_verify。")
            return

        yield event.plain_result("正在验证邀请链接有效性，请稍候...")
        is_valid, msg = await self._verify_invite_link(url)
        status = "✅ 有效" if is_valid else "❌ 无效"
        yield event.plain_result(f"{status}\n链接: {url}\n详情: {msg}")

    @filter.permission_type(filter.PermissionType.ADMIN)
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
        is_group = bool(event.get_group_id())
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
            if is_group:
                # Mask the token part, keep domain visible
                masked = re.sub(r"(invites?|join|register|signup|referral)/\S+", r"\1/****", code, flags=re.IGNORECASE)
                if masked != code:
                    code = masked
                else:
                    code = "****"
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
            invalid_count = self._cleanup_invalid()
        total = expired_count + invalid_count
        yield event.plain_result(
            f"已清理 {expired_count} 个过期 + {invalid_count} 个无效邀请码,"
            f"共 {total} 个,剩余 {len(self.invite_codes)} 个。"
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

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("重置每周限额")
    async def reset_weekly_usage_cmd(self, event: AstrMessageEvent, user_id: str = ""):
        """重置每周邀请码领取记录。用法: /重置每周限额 <QQ号>，留空重置全部。"""
        if user_id:
            week = self._week_str()
            if week in self._weekly_usage and user_id in self._weekly_usage[week]:
                del self._weekly_usage[week][user_id]
                self._save_weekly_usage()
                yield event.plain_result(f"已重置用户 {user_id} 的每周限额。")
            else:
                yield event.plain_result(f"用户 {user_id} 本周无领取记录。")
        else:
            self._weekly_usage = {}
            self._save_weekly_usage()
            yield event.plain_result("已清空所有每周限额记录。")

    async def _detect_invite_intent(self, event: AstrMessageEvent, msg: str) -> bool:
        """Return True if the user specifically wants a Linux.Do (L站) invite."""
        try:
            provider = await self._get_intent_provider(event)
            if provider:
                resp = await provider.text_chat(
                    prompt=(
                        f'群聊中用户说了：「{msg}」\n'
                        f'请判断：该用户是在索要 Linux.Do（又称 L站）的邀请码或注册链接吗？\n'
                        f'\n'
                        f'回复规则：\n'
                        f'- 用户在求 L站/Linux.Do 的邀请码或注册链接 → 回复"是"\n'
                        f'- 用户在求其他社区（如 Nodeloc、Hostloc 等）的邀请码 → 回复"否"\n'
                        f'- 用户只是在讨论、科普、询问邀请码的用途或机制 → 回复"否"\n'
                        f'- 用户提到了 L站/Linux.Do 但不是求邀请码 → 回复"否"\n'
                        f'\n'
                        f'只回复一个字：是 或 否。'
                    ),
                )
                intent = resp.completion_text.strip()
                logger.debug(f"L站邀请码意图判断: msg={msg[:80]} intent={intent}")
                return "是" in intent and "否" not in intent
        except Exception as e:
            logger.debug(f"意图判断失败，回退到直接触发: {e}")
        return True

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

        # Pre-check: only respond if there are valid invites and user has quota
        if not self._pick_random_invite():
            logger.debug("邀请码列表为空或无有效邀请码，忽略触发")
            return

        if not self._check_weekly_limit(event.get_sender_id()):
            logger.debug(f"用户 {event.get_sender_id()} 已达每周限额，忽略触发")
            return

        if not await self._check_qq_level(event):
            min_lv = self.config.get("min_qq_level", 0)
            yield event.plain_result(f"你的 QQ 等级不足 {min_lv} 级，无法获取邀请码。")
            return

        if not await self._check_group_level(event):
            min_gl = self.config.get("min_group_level", "")
            yield event.plain_result(f"你的群活跃等级不足「{min_gl}」，无法获取邀请码。")
            return

        if not await self._detect_invite_intent(event, msg):
            return

        invite = self._pick_random_invite()
        if not invite:
            return

        self._locked_invites.add(invite["id"])

        use_kb = self._use_kb()
        kb_question = self._pick_question() if use_kb else None
        if use_kb and not kb_question:
            # 题库为空，尝试自动生成
            count, _ = await self._generate_question_pool(event)
            if count > 0:
                kb_question = self._pick_question()

        confirm_timeout = self.config.get("confirm_timeout", 10)
        answer_timeout = self.config.get("answer_timeout", 90)
        retry_limit = self.config.get("allow_retry", 3)
        delivery = self.config.get("delivery_method", "private_message")

        expiry_hint = (
            f"（{self._format_expiry(invite)}）" if invite.get("expires_at") else ""
        )
        if kb_question:
            question_text = kb_question["question"]
            reference_answer = kb_question.get("reference_answer", "")
            correct_answer = ""
        else:
            question_text = invite.get("question", self.config.get("default_question", ""))
            correct_answer = invite.get("answer", self.config.get("default_answer", "L站")).strip().lower()
            reference_answer = ""

        session = ChallengeSession(
            invite=invite,
            question_text=question_text,
            reference_answer=reference_answer,
            correct_answer=correct_answer,
            use_kb=use_kb,
            kb_question=kb_question,
            expiry_hint=expiry_hint,
        )

        confirm_prompt = (
            "检测到你可能需要 Linux.Do 邀请码，"
            "是否要进行答题获取？\n回复「是」开始答题，回复「退出」取消。"
        )
        yield event.plain_result(confirm_prompt)

        sender_id = event.get_sender_id()

        try:

            @session_waiter(timeout=confirm_timeout)
            async def waiter(controller: SessionController, e: AstrMessageEvent):
                nonlocal session

                # 只响应发起者，忽略其他人
                if e.get_sender_id() != sender_id:
                    controller.keep(timeout=answer_timeout, reset_timeout=True)
                    return

                text = e.message_str.strip()
                if not text:
                    controller.keep(timeout=answer_timeout, reset_timeout=True)
                    return

                if text == "退出":
                    await e.send(e.plain_result("已取消。"))
                    controller.stop()
                    return

                # Phase 1: 确认是否要答题
                if session.confirm_phase:
                    if text in ("是", "要", "好", "yes", "y", "ok", "嗯", "对", "可以"):
                        session.confirm_phase = False
                        # Trigger auto-refill if KB pool is low
                        if session.use_kb:
                            asyncio.create_task(self._trigger_question_pool_refill(e))
                        question_prompt = (
                            f"【{session.invite['name']}】{session.expiry_hint}\n\n"
                            f"{session.question_text}\n\n直接回复答案，发送「退出」可取消。"
                        )
                        await e.send(e.plain_result(question_prompt))
                        controller.keep(timeout=answer_timeout, reset_timeout=True)
                        return
                    else:
                        # 不回复，静默等待，超时自动取消
                        controller.keep(timeout=answer_timeout, reset_timeout=True)
                        return

                # "重试" just re-sends the question without consuming an attempt
                if text == "重试":
                    question_prompt = (
                        f"【{session.invite['name']}】{session.expiry_hint}\n\n"
                        f"{session.question_text}\n\n直接回复答案，发送「退出」可取消。"
                    )
                    await e.send(e.plain_result(question_prompt))
                    controller.keep(timeout=answer_timeout, reset_timeout=True)
                    return

                session.attempts += 1

                # KB 模式：LLM 判题；非 KB 模式：字符串匹配
                passed = False
                judge_feedback = ""
                if session.use_kb:
                    await e.send(e.plain_result("正在评判你的回答..."))
                    passed, judge_feedback = await self._judge_answer(
                        e, session.question_text, session.reference_answer, text,
                    )
                    if judge_feedback:
                        await e.send(e.plain_result(judge_feedback))
                else:
                    passed = text.lower() == session.correct_answer

                if passed:
                    if self._is_expired(session.invite):
                        await e.send(e.plain_result("抱歉，这个邀请码刚刚过期了。"))
                        controller.stop()
                        return

                    # 发放前二次验证
                    enable_verify = self.config.get("enable_verify", True)
                    if enable_verify:
                        await e.send(e.plain_result("回答正确，正在验证链接有效性..."))
                        is_valid, verify_msg = await self._verify_invite_link(session.invite["code"])
                        if not is_valid:
                            session.invite["verified"] = False
                            session.invite["verify_msg"] = verify_msg
                            self._save_data()
                            new_invite = self._pick_random_invite()
                            if new_invite and new_invite["id"] != session.invite["id"]:
                                session.invite = new_invite
                                session.attempts = 0
                                session.confirm_phase = True
                                # 换题
                                if session.use_kb:
                                    session.kb_question = self._pick_question()
                                if session.kb_question:
                                    session.question_text = session.kb_question["question"]
                                    session.reference_answer = session.kb_question.get("reference_answer", "")
                                else:
                                    session.question_text = session.invite.get("question", self.config.get("default_question", ""))
                                    session.correct_answer = session.invite.get("answer", self.config.get("default_answer", "L站")).strip().lower()
                                session.expiry_hint = (
                                    f"（{self._format_expiry(session.invite)}）"
                                    if session.invite.get("expires_at") else ""
                                )
                                await e.send(e.plain_result(
                                    f"该邀请链接已失效（{verify_msg}），为你更换另一个。\n\n"
                                    "检测到你可能需要 Linux.Do 邀请码，"
                                    "是否要答题获取？回复「是」开始，回复「退出」取消。"
                                ))
                                controller.keep(timeout=answer_timeout, reset_timeout=True)
                                return
                            await e.send(e.plain_result(
                                f"该邀请链接已失效（{verify_msg}），且暂无其他可用邀请码。"
                            ))
                            controller.stop()
                            return

                    if delivery == "email":
                        target_email = await self._resolve_email(e)
                        if not target_email:
                            await e.send(e.plain_result(
                                "无法确定收件邮箱:当前平台不支持自动推断,请联系管理员调整 delivery_method。"
                            ))
                            controller.stop()
                            return
                        try:
                            await self._send_email(target_email, session.invite["code"], session.invite["name"])
                            self._record_weekly_usage(e.get_sender_id())
                            self._consume_invite(session.invite["id"])
                            await e.send(e.plain_result(f"回答正确!邀请码已发送到 {target_email},请查收。"))
                        except Exception as exc:
                            logger.error(f"邮件发送失败: {exc}")
                            await e.send(e.plain_result(f"邮件发送失败: {exc}"))
                        controller.stop()
                        return

                    await e.send(e.plain_result("回答正确,正在私发邀请码,请查看私聊。"))
                    try:
                        await self._send_private_msg(e, session.invite["code"], session.invite["name"])
                        self._record_weekly_usage(e.get_sender_id())
                        self._consume_invite(session.invite["id"])
                    except Exception:
                        await e.send(e.plain_result(
                            "私发失败,请确认已添加机器人为好友,或联系管理员。"
                        ))
                    controller.stop()
                    return

                remaining = retry_limit - session.attempts if retry_limit > 0 else None
                if retry_limit > 0 and remaining <= 0:
                    if session.use_kb:
                        await e.send(e.plain_result(
                            "已达最大重试次数。请重新发送关键词发起新请求。"
                        ))
                    else:
                        await e.send(e.plain_result(
                            f"回答错误，已达最大重试次数。正确答案是「{session.session.correct_answer}」。"
                            f"请重新发送关键词发起新请求。"
                        ))
                    controller.stop()
                    return

                hint = f"还可重试 {remaining} 次" if remaining else "请再试一次"
                await e.send(e.plain_result(f"回答错误，{hint}。发送「退出」可取消。"))

            await waiter(event)
        except TimeoutError:
            if session.use_kb:
                yield event.plain_result("验证超时，请重新发送关键词发起新请求。")
            else:
                yield event.plain_result(f"验证超时。正确答案是「{session.correct_answer}」。")
        except Exception as exc:
            logger.error(f"邀请码验证流程异常: {exc}", exc_info=True)
            yield event.plain_result("验证流程出错，请稍后再试。")
        finally:
            if invite:
                self._locked_invites.discard(invite["id"])
            event.stop_event()

    # ========== LLM Tool: Get Invite Question ==========

    def _new_challenge_token(self) -> str:
        return secrets.token_urlsafe(16)

    def _gc_pending_challenges(self):
        now = self._now_ts()
        for tok in list(self._pending_challenges.keys()):
            challenge = self._pending_challenges[tok]
            if challenge.get("expires_at", 0) < now:
                self._locked_invites.discard(challenge.get("invite_id"))
                self._pending_challenges.pop(tok, None)

    @filter.llm_tool(name="get_invite_question")
    async def llm_get_invite_question(self, event: AstrMessageEvent):
        """Get a random invite code challenge question for the current user.

        Call this when a user asks for a registration link or invite code.
        The returned JSON contains:
        - challenge_token (str): session token to pass to check_invite_answer
        - name (str): human-readable name of the invite
        - question (str): the verification question to ask the user
        - expires_at (float|None): UNIX timestamp when the invite expires
        - instructions (str): guidance for the LLM on what to do next

        DO NOT try to guess or infer the answer. Always call check_invite_answer
        with the user's response and the challenge_token.
        """
        self._cleanup_expired()
        self._gc_pending_challenges()

        if not self._check_weekly_limit(event.get_sender_id()):
            return "该用户本周已达获取上限，请告知用户下周再来。"

        if not await self._check_qq_level(event):
            min_lv = self.config.get("min_qq_level", 0)
            return f"该用户的 QQ 等级不足 {min_lv} 级，无法获取邀请码。"

        invite = self._pick_random_invite()
        if not invite:
            return "暂无可用的邀请码。引导用户私聊机器人发送邀请链接。"

        self._locked_invites.add(invite["id"])

        use_kb = self._use_kb()
        if use_kb:
            kb_question = self._pick_question()
            if not kb_question:
                return "题库为空,请联系管理员使用「/刷新题库」生成题目。"
            question = kb_question["question"]
            reference = kb_question.get("reference_answer", "")
        else:
            question = invite.get("question", self.config.get("default_question", ""))
            reference = invite.get("answer", self.config.get("default_answer", "L站"))

        token = self._new_challenge_token()
        ttl = max(int(self.config.get("answer_timeout", 90) + 30), 60)
        self._pending_challenges[token] = {
            "invite_id": invite["id"],
            "question": question,
            "reference_answer": reference,
            "kb_mode": use_kb,
            "user_id": event.get_sender_id(),
            "expires_at": self._now_ts() + ttl,
        }
        return json.dumps(
            {
                "challenge_token": token,
                "name": invite["name"],
                "question": question,
                "expires_at": invite.get("expires_at"),
                "instructions": (
                    "向用户提问 question 字段内容,等待用户回答后,调用 check_invite_answer "
                    "并把 challenge_token 与用户原话作为 answer 传入。不要自行判断对错。"
                ),
            },
            ensure_ascii=False,
        )

    # ========== LLM Tool: Check Answer & Deliver ==========

    @filter.llm_tool(name="check_invite_answer")
    async def llm_check_invite_answer(
        self, event: AstrMessageEvent, challenge_token: str, answer: str,
    ):
        """Validate the user's answer and deliver the invite code if correct.

        Args:
            challenge_token (str): session token from get_invite_question
            answer (str): the user's verbatim response to the challenge question

        Returns:
            str: feedback message. On success, the invite code is sent to the
                 user via private message or email (depending on config).
                 On failure, describes what went wrong.
        """
        self._gc_pending_challenges()
        challenge = self._pending_challenges.get(challenge_token)
        if not challenge:
            return "challenge_token 无效或已过期,请重新调用 get_invite_question。"
        if challenge.get("user_id") and challenge["user_id"] != event.get_sender_id():
            return "该 challenge_token 不属于当前用户,请重新调用 get_invite_question。"

        invite = self._get_invite_by_id(challenge["invite_id"])
        if not invite:
            self._pending_challenges.pop(challenge_token, None)
            return "该邀请码不存在或已过期,请重新调用 get_invite_question。"

        kb_mode = challenge["kb_mode"]
        question = challenge["question"]
        reference = challenge["reference_answer"]

        if kb_mode:
            passed, feedback = await self._judge_answer(event, question, reference, answer)
            if not passed:
                return feedback or "回答错误,请引导用户再试一次。"
        elif answer.strip().lower() != reference.strip().lower():
            return "回答错误,请引导用户再试一次。"

        # 答对即消耗 token
        self._pending_challenges.pop(challenge_token, None)
        self._locked_invites.discard(invite["id"])

        if self._is_expired(invite):
            return "该邀请码已过期,请重新调用 get_invite_question。"

        # 发放前二次验证
        enable_verify = self.config.get("enable_verify", True)
        if enable_verify:
            is_valid, verify_msg = await self._verify_invite_link(invite["code"])
            if not is_valid:
                invite["verified"] = False
                invite["verify_msg"] = verify_msg
                self._save_data()
                new_invite = self._pick_random_invite()
                if new_invite and new_invite["id"] != invite["id"]:
                    return (
                        f"该链接已失效({verify_msg}),已自动剔除。"
                        f"请重新调用 get_invite_question 获取新题目。"
                    )
                return "该链接已失效,且暂无其他可用邀请码。"

        delivery = self.config.get("delivery_method", "private_message")
        if delivery == "email":
            target_email = await self._resolve_email(event)
            if not target_email:
                return (
                    "回答正确,但当前配置为邮箱发送且当前平台无法自动推断邮箱。"
                    "请引导用户提供邮箱地址,或由管理员通过私聊手动发送。"
                )
            try:
                await self._send_email(target_email, invite["code"], invite["name"])
                self._record_weekly_usage(event.get_sender_id())
                self._consume_invite(invite["id"])
                return f"邀请码已发送至 {target_email},请告知用户查收邮件。"
            except Exception as exc:
                logger.error(f"LLM tool 邮件发送失败: {exc}")
                return f"邮件发送失败: {exc}。请告知用户联系管理员。"

        try:
            await self._send_private_msg(event, invite["code"], invite["name"])
            self._record_weekly_usage(event.get_sender_id())
            self._consume_invite(invite["id"])
        except Exception as exc:
            logger.error(f"LLM tool 私发邀请码失败: {exc}")
            return f"私发失败: {exc}。请告知用户确认已添加机器人为好友。"

        return "邀请码已通过私聊发送给用户,请告知用户查看私聊。"
