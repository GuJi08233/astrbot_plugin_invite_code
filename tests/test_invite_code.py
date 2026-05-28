"""Tests for astrbot_plugin_invite_code."""

import asyncio
import json
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Add plugin dir to path
plugin_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(plugin_dir))

from main import SITE_VERIFY_RULES, ChallengeSession, InviteCodePlugin  # noqa: E402


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
        self._patch = patch.object(InviteCodePlugin, "__init__", lambda self, *a, **kw: None)
        self._patch.start()
        self.plugin = InviteCodePlugin.__new__(InviteCodePlugin)
        self.plugin.config = MagicMock()
        self.plugin.data_dir = MagicMock()
        self.plugin.invite_codes = []
        self.plugin.question_pool = []
        self.plugin._daily_usage = {}
        self.plugin._pending_challenges = {}

    def tearDown(self):
        self._patch.stop()

    def test_now_ts(self):
        ts = self.plugin._now_ts()
        self.assertIsInstance(ts, float)
        self.assertAlmostEqual(ts, time.time(), delta=1)

    def test_today_str(self):
        today = self.plugin._today_str()
        import datetime
        expected = datetime.date.today().isoformat()
        self.assertEqual(today, expected)

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
        self.plugin.data_file = MagicMock()
        self.plugin.data_file.exists.return_value = False
        removed = self.plugin._cleanup_expired()
        self.assertEqual(removed, 1)
        self.assertEqual(len(self.plugin.invite_codes), 1)
        self.assertEqual(self.plugin.invite_codes[0]["id"], 2)

    def test_cleanup_invalid(self):
        self.plugin.invite_codes = [
            {"id": 1, "verified": True},
            {"id": 2, "verified": False},
            {"id": 3, "verified": None},
            {"id": 4},
        ]
        self.plugin.data_file = MagicMock()
        self.plugin.data_file.exists.return_value = False
        removed = self.plugin._cleanup_invalid()
        self.assertEqual(removed, 1)
        ids = [e["id"] for e in self.plugin.invite_codes]
        self.assertIn(1, ids)
        self.assertNotIn(2, ids)
        self.assertIn(3, ids)
        self.assertIn(4, ids)

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

    def test_check_daily_limit_no_limit(self):
        self.plugin.config.get.return_value = 0
        result = self.plugin._check_daily_limit("user1")
        self.assertTrue(result)

    def test_check_daily_limit_under(self):
        self.plugin.config.get.return_value = 3
        today = self.plugin._today_str()
        self.plugin._daily_usage = {today: {"user1": 2}}
        result = self.plugin._check_daily_limit("user1")
        self.assertTrue(result)

    def test_check_daily_limit_reached(self):
        self.plugin.config.get.return_value = 1
        today = self.plugin._today_str()
        self.plugin._daily_usage = {today: {"user1": 1}}
        result = self.plugin._check_daily_limit("user1")
        self.assertFalse(result)

    def test_record_daily_usage(self):
        self.plugin.config.get.return_value = 3
        self.plugin._record_daily_usage("user1")
        today = self.plugin._today_str()
        self.assertEqual(self.plugin._daily_usage[today]["user1"], 1)
        self.plugin._record_daily_usage("user1")
        self.assertEqual(self.plugin._daily_usage[today]["user1"], 2)

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
        self.plugin.config.get.return_value = "pass"
        self.assertTrue(self.plugin._verify_fail_open())

    def test_verify_fail_open_false(self):
        self.plugin.config.get.return_value = "reject"
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
        self.plugin.config.get.return_value = []
        self.assertFalse(self.plugin._use_kb())

    def test_use_kb_configured(self):
        self.plugin.config.get.return_value = ["Linux.Do"]
        self.assertTrue(self.plugin._use_kb())

    def test_pick_question_empty(self):
        self.plugin.question_pool = []
        result = self.plugin._pick_question()
        self.assertIsNone(result)

    def test_pick_question_has_pool(self):
        self.plugin.question_pool = [{"id": 1, "question": "Q1"}]
        result = self.plugin._pick_question()
        self.assertIsNotNone(result)
        self.assertEqual(result["question"], "Q1")


class TestDataPersistence(unittest.TestCase):
    """Tests for JSON data loading."""

    def setUp(self):
        self._patch = patch.object(InviteCodePlugin, "__init__", lambda self, *a, **kw: None)
        self._patch.start()
        self.plugin = InviteCodePlugin.__new__(InviteCodePlugin)
        self.plugin.config = MagicMock()
        self.plugin.data_dir = MagicMock()
        self.plugin.invite_codes = []
        self.plugin.question_pool = []
        self.plugin._daily_usage = {}
        self.tmp_dir = MagicMock()

    def tearDown(self):
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
