"""Tests for astrbot_plugin_invite_code."""

import asyncio
import importlib.util
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

# Add plugin dir to path
plugin_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(plugin_dir))

spec = importlib.util.spec_from_file_location(
    "astrbot_plugin_invite_code_main",
    plugin_dir / "main.py",
)
plugin_module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules[spec.name] = plugin_module
spec.loader.exec_module(plugin_module)

SITE_VERIFY_RULES = plugin_module.SITE_VERIFY_RULES
ChallengeSession = plugin_module.ChallengeSession
InviteCodePlugin = plugin_module.InviteCodePlugin


class FakeProviderManager:
    async def get_provider_by_id(self, _provider_id):
        return None


class FakeProvider:
    def __init__(self, completion_text):
        self.completion_text = completion_text
        self.prompts = []

    async def text_chat(self, prompt):
        self.prompts.append(prompt)
        return SimpleNamespace(completion_text=self.completion_text)


class FakeKbManager:
    async def retrieve(self, **_kwargs):
        return {
            "context_text": (
                "社区要求新人阅读规则后再发帖。禁止灌水、重复推广和泄露隐私。"
                "遇到可疑内容应先举报并等待处理。"
            )
        }


class FakeContext:
    def __init__(self, provider, send_result=True):
        self.provider = provider
        self.send_result = send_result
        self.sent = []
        self.kb_manager = FakeKbManager()
        self.provider_manager = FakeProviderManager()

    def get_using_provider(self, umo=None):
        return self.provider

    async def send_message(self, session, message_chain):
        text = "".join(getattr(comp, "text", "") for comp in message_chain.chain)
        self.sent.append((session, text))
        return self.send_result


class FakeEvent:
    unified_msg_origin = "aiocqhttp:GroupMessage:123"

    def __init__(self, sender_id="user1"):
        self.sender_id = sender_id

    def get_sender_id(self):
        return self.sender_id

    def get_platform_name(self):
        return "aiocqhttp"

    def get_group_id(self):
        return "123"


class TestChallengeSession(unittest.TestCase):
    """Tests for the ChallengeSession dataclass."""

    def test_defaults(self):
        s = ChallengeSession()
        self.assertEqual(s.attempts, 0)
        self.assertTrue(s.confirm_phase)
        self.assertIsNone(s.invite)
        self.assertEqual(s.question_text, "")
        self.assertFalse(s.use_kb)

    def test_custom_values(self):
        invite = {"id": 1, "name": "test"}
        s = ChallengeSession(
            invite=invite,
            question_text="What is Linux?",
            correct_answer="An OS",
            use_kb=True,
        )
        self.assertEqual(s.invite["name"], "test")
        self.assertEqual(s.question_text, "What is Linux?")
        self.assertTrue(s.use_kb)

    def test_mutation(self):
        s = ChallengeSession()
        s.attempts += 1
        s.confirm_phase = False
        s.invite = {"id": 2}
        self.assertEqual(s.attempts, 1)
        self.assertFalse(s.confirm_phase)
        self.assertEqual(s.invite["id"], 2)


class TestPluginUtils(unittest.TestCase):
    """Tests for static/standalone utility methods."""

    def setUp(self):
        self._patch = patch.object(
            InviteCodePlugin, "__init__", lambda self, *a, **kw: None
        )
        self._patch.start()
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.plugin = InviteCodePlugin.__new__(InviteCodePlugin)
        self.plugin.config = {}
        self.plugin.data_dir = Path(self._tmp_dir.name)
        self.plugin.data_file = self.plugin.data_dir / "invite_codes.json"
        self.plugin.question_file = self.plugin.data_dir / "question_pool.json"
        self.plugin.weekly_usage_file = self.plugin.data_dir / "weekly_usage.json"
        self.plugin.invite_codes = []
        self.plugin.question_pool = []
        self.plugin._weekly_usage = {}
        self.plugin._pending_challenges = {}
        self.plugin._locked_invites = set()
        self.plugin._question_pool_refilling = False

    def tearDown(self):
        self._tmp_dir.cleanup()
        self._patch.stop()

    def test_now_ts(self):
        ts = self.plugin._now_ts()
        self.assertIsInstance(ts, float)
        self.assertAlmostEqual(ts, time.time(), delta=1)

    def test_week_str(self):
        week = self.plugin._week_str()
        import datetime

        iso = datetime.date.today().isocalendar()
        expected = f"{iso[0]}-W{iso[1]:02d}"
        self.assertEqual(week, expected)

    def test_is_expired_no_expiry(self):
        entry = {"id": 1}
        self.assertFalse(self.plugin._is_expired(entry))

    def test_is_expired_future(self):
        future = time.time() + 3600
        entry = {"id": 1, "expires_at": future}
        self.assertFalse(self.plugin._is_expired(entry))

    def test_is_expired_past(self):
        past = time.time() - 3600
        entry = {"id": 1, "expires_at": past}
        self.assertTrue(self.plugin._is_expired(entry))

    def test_cleanup_expired(self):
        past = time.time() - 100
        future = time.time() + 100
        self.plugin.invite_codes = [
            {"id": 1, "expires_at": past},
            {"id": 2, "expires_at": future},
        ]
        self.plugin._locked_invites = {1, 2, 99}
        self.plugin.data_file = MagicMock()
        self.plugin.data_file.exists.return_value = False
        removed = self.plugin._cleanup_expired()
        self.assertEqual(removed, 1)
        self.assertEqual(len(self.plugin.invite_codes), 1)
        self.assertEqual(self.plugin.invite_codes[0]["id"], 2)
        self.assertEqual(self.plugin._locked_invites, {2})

    def test_cleanup_invalid(self):
        self.plugin.invite_codes = [
            {"id": 1, "verified": True},
            {"id": 2, "verified": False},
            {"id": 3, "verified": None},
            {"id": 4},
        ]
        self.plugin._locked_invites = {1, 2, 4}
        self.plugin.data_file = MagicMock()
        self.plugin.data_file.exists.return_value = False
        removed = self.plugin._cleanup_invalid()
        self.assertEqual(removed, 1)
        ids = [e["id"] for e in self.plugin.invite_codes]
        self.assertIn(1, ids)
        self.assertNotIn(2, ids)
        self.assertIn(3, ids)
        self.assertIn(4, ids)
        self.assertEqual(self.plugin._locked_invites, {1, 4})

    def test_format_expiry_none(self):
        entry = {"expires_at": None}
        result = self.plugin._format_expiry(entry)
        self.assertEqual(result, "永不过期")

    def test_format_expiry_future(self):
        future = time.time() + 7200
        entry = {"expires_at": future}
        result = self.plugin._format_expiry(entry)
        self.assertIn("剩余", result)
        self.assertIn("2h", result)

    def test_format_expiry_past(self):
        past = time.time() - 100
        entry = {"expires_at": past}
        result = self.plugin._format_expiry(entry)
        self.assertEqual(result, "已过期")

    def test_clean_invite_url_strips_trailing_punctuation(self):
        url = self.plugin._clean_invite_url("https://linux.do/invites/abc123）。")
        self.assertEqual(url, "https://linux.do/invites/abc123")

    def test_normalize_answer_ignores_space_and_case(self):
        self.assertEqual(
            self.plugin._normalize_answer(" L 站 "),
            self.plugin._normalize_answer("l站"),
        )

    def test_as_llm_bool_treats_false_string_as_false(self):
        self.assertFalse(self.plugin._as_llm_bool("false"))
        self.assertFalse(self.plugin._as_llm_bool("否"))
        self.assertTrue(self.plugin._as_llm_bool("true"))
        self.assertTrue(self.plugin._as_llm_bool("是"))

    def test_check_weekly_limit_no_limit(self):
        self.plugin.config = {"weekly_limit": 0}
        result = self.plugin._check_weekly_limit("user1")
        self.assertTrue(result)

    def test_check_weekly_limit_under(self):
        self.plugin.config = {"weekly_limit": 3}
        week = self.plugin._week_str()
        self.plugin._weekly_usage = {week: {"user1": 2}}
        result = self.plugin._check_weekly_limit("user1")
        self.assertTrue(result)

    def test_check_weekly_limit_reached(self):
        self.plugin.config = {"weekly_limit": 1}
        week = self.plugin._week_str()
        self.plugin._weekly_usage = {week: {"user1": 1}}
        result = self.plugin._check_weekly_limit("user1")
        self.assertFalse(result)

    def test_record_weekly_usage(self):
        self.plugin.config = {"weekly_limit": 3}
        self.plugin._record_weekly_usage("user1")
        week = self.plugin._week_str()
        self.assertEqual(self.plugin._weekly_usage[week]["user1"], 1)
        self.plugin._record_weekly_usage("user1")
        self.assertEqual(self.plugin._weekly_usage[week]["user1"], 2)

    def test_new_challenge_token(self):
        token1 = self.plugin._new_challenge_token()
        token2 = self.plugin._new_challenge_token()
        self.assertIsInstance(token1, str)
        self.assertGreater(len(token1), 10)
        self.assertNotEqual(token1, token2)

    def test_gc_pending_challenges(self):
        now = time.time()
        self.plugin._pending_challenges = {
            "valid": {"expires_at": now + 300},
            "expired": {"expires_at": now - 100},
        }
        self.plugin._gc_pending_challenges()
        self.assertIn("valid", self.plugin._pending_challenges)
        self.assertNotIn("expired", self.plugin._pending_challenges)

    def test_verify_fail_open_true(self):
        self.plugin.config = {"verify_fail_mode": "pass"}
        self.assertTrue(self.plugin._verify_fail_open())

    def test_verify_fail_open_false(self):
        self.plugin.config = {"verify_fail_mode": "reject"}
        self.assertFalse(self.plugin._verify_fail_open())

    def test_pick_random_invite_empty(self):
        self.plugin.invite_codes = []
        result = self.plugin._pick_random_invite()
        self.assertIsNone(result)

    def test_pick_random_invite_skips_expired(self):
        past = time.time() - 100
        future = time.time() + 3600
        self.plugin.invite_codes = [
            {"id": 1, "expires_at": past},
            {"id": 2, "expires_at": future, "verified": True},
        ]
        result = self.plugin._pick_random_invite()
        self.assertEqual(result["id"], 2)

    def test_pick_random_invite_skips_invalid(self):
        self.plugin.invite_codes = [
            {"id": 1, "verified": False},
            {"id": 2, "verified": True},
        ]
        result = self.plugin._pick_random_invite()
        self.assertEqual(result["id"], 2)

    def test_consume_invite(self):
        self.plugin.invite_codes = [
            {"id": 1, "name": "a"},
            {"id": 2, "name": "b"},
        ]
        self.plugin.data_file = MagicMock()
        self.plugin.data_file.exists.return_value = False
        result = self.plugin._consume_invite(1)
        self.assertTrue(result)
        self.assertEqual(len(self.plugin.invite_codes), 1)

    def test_consume_invite_not_found(self):
        self.plugin.invite_codes = [{"id": 1}]
        self.plugin.data_file = MagicMock()
        self.plugin.data_file.exists.return_value = False
        result = self.plugin._consume_invite(999)
        self.assertFalse(result)

    def test_get_invite_by_id(self):
        self.plugin.invite_codes = [{"id": 1, "name": "test"}]
        result = self.plugin._get_invite_by_id(1)
        self.assertEqual(result["name"], "test")

    def test_get_invite_by_id_expired(self):
        past = time.time() - 100
        self.plugin.invite_codes = [{"id": 1, "expires_at": past}]
        result = self.plugin._get_invite_by_id(1)
        self.assertIsNone(result)

    def test_detect_site_rule_match(self):
        result = self.plugin._detect_site_rule("https://linux.do/invites/abc123")
        self.assertIsNotNone(result)
        self.assertEqual(result["desc"], "LINUX DO 邀请链接")

    def test_detect_site_rule_no_match(self):
        result = self.plugin._detect_site_rule("https://example.com/invite")
        self.assertIsNone(result)

    def test_make_private_umo(self):
        event = MagicMock()
        event.get_platform_name.return_value = "aiocqhttp"
        event.get_sender_id.return_value = "12345"
        result = InviteCodePlugin._make_private_umo(event)
        self.assertEqual(result, "aiocqhttp:friend:12345")

    def test_resolve_email_qq(self):
        event = MagicMock()
        event.get_platform_name.return_value = "aiocqhttp"
        event.get_sender_id.return_value = "123456"
        result = asyncio.run(self.plugin._resolve_email(event))
        self.assertEqual(result, "123456@qq.com")

    def test_resolve_email_other_platform(self):
        event = MagicMock()
        event.get_platform_name.return_value = "telegram"
        event.get_sender_id.return_value = "123"
        result = asyncio.run(self.plugin._resolve_email(event))
        self.assertIsNone(result)

    def test_use_kb_empty(self):
        self.plugin.config = {"kb_names": []}
        self.assertFalse(self.plugin._use_kb())

    def test_use_kb_configured(self):
        self.plugin.config = {"kb_names": ["Linux.Do"]}
        self.assertTrue(self.plugin._use_kb())

    def test_pick_question_empty(self):
        self.plugin.question_pool = []
        result = self.plugin._pick_question()
        self.assertIsNone(result)

    def test_pick_question_has_pool(self):
        self.plugin.question_pool = [
            {"id": 1, "question": "Q1", "reference_answer": "A1"}
        ]
        result = self.plugin._pick_question()
        self.assertIsNotNone(result)
        self.assertEqual(result["question"], "Q1")

    def test_question_key_normalizes_punctuation_and_spaces(self):
        left = self.plugin._question_key(" 社区 规则是什么？ ")
        right = self.plugin._question_key("社区规则是什么")
        self.assertEqual(left, right)

    def test_normalize_question_pool_drops_duplicates_and_invalid_items(self):
        self.plugin.question_pool = [
            {"question": "社区规则是什么？", "reference_answer": "阅读规则"},
            {"question": "社区规则是什么", "reference_answer": "重复题"},
            {"question": "", "reference_answer": "空题"},
            {"question": "如何举报？", "reference_answer": "使用举报功能"},
        ]
        removed = self.plugin._normalize_question_pool()
        self.assertEqual(removed, 2)
        self.assertEqual(len(self.plugin.question_pool), 2)
        self.assertEqual([q["id"] for q in self.plugin.question_pool], [1, 2])

    def test_pick_question_rotates_by_usage(self):
        self.plugin.question_pool = [
            {"id": 1, "question": "Q1", "reference_answer": "A1"},
            {"id": 2, "question": "Q2", "reference_answer": "A2"},
        ]

        first = self.plugin._pick_question()
        second = self.plugin._pick_question()

        self.assertEqual(first["question"], "Q1")
        self.assertEqual(second["question"], "Q2")
        self.assertTrue(self.plugin._question_pool_rotation_complete())


class TestQuestionGeneration(unittest.IsolatedAsyncioTestCase):
    """Tests for KB question generation and pool merging."""

    def setUp(self):
        self._patch = patch.object(
            InviteCodePlugin, "__init__", lambda self, *a, **kw: None
        )
        self._patch.start()
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.plugin = InviteCodePlugin.__new__(InviteCodePlugin)
        self.plugin.config = {
            "kb_names": ["Linux.Do"],
            "question_pool_size": 3,
            "question_pool_min_size": 1,
            "question_gen_prompt_template": (
                "只输出 JSON 数组，每个元素包含 question、reference_answer、type。"
            ),
        }
        self.plugin.data_dir = Path(self._tmp_dir.name)
        self.plugin.data_file = self.plugin.data_dir / "invite_codes.json"
        self.plugin.question_file = self.plugin.data_dir / "question_pool.json"
        self.plugin.weekly_usage_file = self.plugin.data_dir / "weekly_usage.json"
        self.plugin.invite_codes = []
        self.plugin.question_pool = []
        self.plugin._weekly_usage = {}
        self.plugin._pending_challenges = {}
        self.plugin._locked_invites = set()
        self.plugin._question_pool_refilling = False

    def tearDown(self):
        self._tmp_dir.cleanup()
        self._patch.stop()

    def test_parse_generated_questions_accepts_code_fence_and_wrapper(self):
        text = """```json
{"questions": [{"question": "Q1", "reference_answer": "A1"}]}
```"""
        questions = self.plugin._parse_generated_questions(text)
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0]["question"], "Q1")

    async def test_generate_question_pool_filters_duplicates_and_invalid_items(self):
        provider = FakeProvider(
            json.dumps(
                [
                    {
                        "question": "社区规则是什么？",
                        "reference_answer": "需要阅读并遵守社区规则。",
                        "type": "knowledge",
                    },
                    {
                        "question": "社区规则是什么",
                        "reference_answer": "重复题应被去掉。",
                        "type": "knowledge",
                    },
                    {"question": "没有答案的题"},
                    {
                        "question": "遇到违规推广应该怎么办？",
                        "reference_answer": "应举报或按社区规则处理。",
                        "type": "case",
                    },
                ],
                ensure_ascii=False,
            )
        )
        self.plugin.context = FakeContext(provider)

        count, err = await self.plugin._generate_question_pool()

        self.assertEqual(err, "")
        self.assertEqual(count, 2)
        self.assertEqual(len(self.plugin.question_pool), 2)
        keys = [q["question_key"] for q in self.plugin.question_pool]
        self.assertEqual(len(keys), len(set(keys)))

    async def test_generate_question_pool_avoids_existing_questions_on_refresh(self):
        self.plugin.question_pool = [
            {
                "question": "社区规则是什么？",
                "reference_answer": "旧答案",
                "type": "knowledge",
            }
        ]
        provider = FakeProvider(
            json.dumps(
                [
                    {
                        "question": "社区规则是什么",
                        "reference_answer": "旧题改写，应跳过。",
                        "type": "knowledge",
                    },
                    {
                        "question": "新人发帖前应该做什么？",
                        "reference_answer": "应先阅读规则并确认发帖要求。",
                        "type": "knowledge",
                    },
                ],
                ensure_ascii=False,
            )
        )
        self.plugin.context = FakeContext(provider)

        count, err = await self.plugin._generate_question_pool(replace=True)

        self.assertEqual(err, "")
        self.assertEqual(count, 1)
        self.assertEqual(
            self.plugin.question_pool[0]["question"], "新人发帖前应该做什么？"
        )
        self.assertIn("已经存在或近期用过", provider.prompts[0])

    async def test_trigger_question_pool_refill_replaces_after_full_rotation(self):
        self.plugin.config["question_refresh_after_uses"] = 1
        self.plugin.question_pool = [
            {
                "question": "旧题是什么？",
                "reference_answer": "旧答案",
                "type": "knowledge",
                "use_count": 1,
            }
        ]
        provider = FakeProvider(
            json.dumps(
                [
                    {
                        "question": "新题应该如何回答？",
                        "reference_answer": "应根据新题评判要点回答。",
                        "type": "knowledge",
                    }
                ],
                ensure_ascii=False,
            )
        )
        self.plugin.context = FakeContext(provider)

        await self.plugin._trigger_question_pool_refill(FakeEvent())

        self.assertEqual(len(self.plugin.question_pool), 1)
        self.assertEqual(self.plugin.question_pool[0]["question"], "新题应该如何回答？")
        self.assertEqual(self.plugin.question_pool[0]["use_count"], 0)

    async def test_llm_get_invite_question_releases_locked_invite_when_generation_fails(
        self,
    ):
        self.plugin.invite_codes = [
            {
                "id": 1,
                "name": "Linux.Do",
                "code": "https://linux.do/invites/abc",
                "verified": True,
            }
        ]
        provider = FakeProvider("[]")
        self.plugin.context = FakeContext(provider)

        result = await self.plugin.llm_get_invite_question(FakeEvent())

        self.assertIn("自动生成失败", result)
        self.assertNotIn(1, self.plugin._locked_invites)

    async def test_llm_check_invite_answer_releases_lock_when_invite_is_gone(self):
        self.plugin.invite_codes = [
            {
                "id": 1,
                "name": "Linux.Do",
                "code": "https://linux.do/invites/abc",
                "expires_at": time.time() - 10,
            }
        ]
        self.plugin._pending_challenges = {
            "token": {
                "invite_id": 1,
                "question": "Q",
                "reference_answer": "A",
                "kb_mode": False,
                "user_id": "user1",
                "expires_at": time.time() + 60,
            }
        }
        self.plugin._locked_invites = {1}
        self.plugin.context = FakeContext(FakeProvider(""))

        result = await self.plugin.llm_check_invite_answer(FakeEvent(), "token", "A")

        self.assertIn("不存在或已过期", result)
        self.assertNotIn(1, self.plugin._locked_invites)

    async def test_judge_answer_string_false_is_not_truthy(self):
        provider = FakeProvider('{"correct": "false", "feedback": "不正确"}')
        self.plugin.context = FakeContext(provider)

        passed, feedback = await self.plugin._judge_answer(
            FakeEvent(),
            "题目",
            "正确答案",
            "错误答案",
        )

        self.assertFalse(passed)
        self.assertEqual(feedback, "不正确")

    async def test_send_private_msg_raises_when_context_send_returns_false(self):
        self.plugin.context = FakeContext(FakeProvider(""), send_result=False)

        with self.assertRaisesRegex(RuntimeError, "未找到私聊目标会话"):
            await self.plugin._send_private_msg(
                FakeEvent(),
                "https://linux.do/invites/abc",
                "Linux.Do",
            )


class TestDataPersistence(unittest.TestCase):
    """Tests for JSON data loading."""

    def setUp(self):
        self._patch = patch.object(
            InviteCodePlugin, "__init__", lambda self, *a, **kw: None
        )
        self._patch.start()
        self._tmp_dir = tempfile.TemporaryDirectory()
        self.plugin = InviteCodePlugin.__new__(InviteCodePlugin)
        self.plugin.config = {}
        self.plugin.data_dir = Path(self._tmp_dir.name)
        self.plugin.data_file = self.plugin.data_dir / "invite_codes.json"
        self.plugin.question_file = self.plugin.data_dir / "question_pool.json"
        self.plugin.weekly_usage_file = self.plugin.data_dir / "weekly_usage.json"
        self.plugin.invite_codes = []
        self.plugin.question_pool = []
        self.plugin._weekly_usage = {}
        self.plugin._pending_challenges = {}
        self.plugin._locked_invites = set()

    def tearDown(self):
        self._tmp_dir.cleanup()
        self._patch.stop()

    def test_load_data_empty(self):
        mock_file = MagicMock()
        mock_file.exists.return_value = False
        self.plugin.data_file = mock_file
        self.plugin._load_data()
        self.assertEqual(self.plugin.invite_codes, [])

    def test_load_data_with_content(self):
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            json.dump([{"id": 1, "name": "test"}], f)
        try:
            mock_file = MagicMock()
            mock_file.exists.return_value = True
            mock_file.__fspath__.return_value = f.name
            self.plugin.data_file = Path(f.name)
            self.plugin._load_data()
            self.assertEqual(len(self.plugin.invite_codes), 1)
            self.assertEqual(self.plugin.invite_codes[0]["name"], "test")
        finally:
            os.unlink(f.name)

    def test_load_data_broken_json(self):
        import os
        import tempfile

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False, encoding="utf-8"
        ) as f:
            f.write("{broken")
        try:
            self.plugin.data_file = Path(f.name)
            self.plugin._load_data()
            self.assertEqual(self.plugin.invite_codes, [])
        finally:
            os.unlink(f.name)


class TestSiteVerifyRules(unittest.TestCase):
    """Tests for SITE_VERIFY_RULES constant."""

    def test_linux_do_rule_exists(self):
        rule = SITE_VERIFY_RULES[0]
        self.assertEqual(rule["desc"], "LINUX DO 邀请链接")
        self.assertIn("Welcome to LINUX DO", rule["valid_text"])
        self.assertIn("expired", rule["error_texts"])


if __name__ == "__main__":
    unittest.main()
