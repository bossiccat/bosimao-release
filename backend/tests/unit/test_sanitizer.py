"""sanitizer 脱敏单测（backend-brain-spec §4.3）

五类用例：路径/代码/API key/邮箱/长文本；断言替换结果不含原文 20 字符子串。
"""
from __future__ import annotations

from app.brain.sanitizer import sanitize


class TestPaths:
    def test_windows_path_replaced(self):
        text = "请重构 C:\\Users\\admin\\secret\\project\\src\\data 目录"
        out = sanitize(text)
        assert "[路径]" in out
        assert "C:\\Users\\admin\\secret" not in out

    def test_unix_path_replaced(self):
        text = "修改 /home/deploy/app/config/settings.py 和 /Users/dev/tmp/a.py"
        out = sanitize(text)
        assert "[路径]" in out
        assert "/home/deploy/app/config" not in out
        assert "/Users/dev/tmp/a.py" not in out

    def test_git_bash_path_replaced(self):
        text = "在 /c/work/project 下执行"
        out = sanitize(text)
        assert "/c/work/project" not in out


class TestCode:
    def test_code_block_replaced(self):
        code = (
            "def refactor_data():\n"
            "    engine = create_engine('postgresql://db')\n"
            "    return engine.execute('SELECT 1')\n"
        )
        text = f"以下是参考代码：\n{code}\n请按此重构"
        out = sanitize(text)
        assert "[代码片段]" in out
        assert "create_engine" not in out  # 原文 20 字符子串被替换

    def test_js_code_block_replaced(self):
        code = "const handler = (req, res) => { res.json({ ok: true }); };"
        text = f"代码如下：{code} 结束"
        out = sanitize(text)
        assert "[代码片段]" in out
        assert "res.json" not in out

    def test_normal_text_kept(self):
        text = "帮我优化一下性能，重点关注查询速度。"
        assert sanitize(text) == text


class TestKeys:
    def test_sk_prefix_replaced(self):
        text = "使用密钥 sk-abc123XYZ7890abcdef1234 调用"
        out = sanitize(text)
        assert "[密钥]" in out
        assert "sk-abc123XYZ7890abcdef1234" not in out

    def test_ghp_token_replaced(self):
        text = "token=ghp_abcdefghijklmnopqrstuvwxyz123456789012"
        out = sanitize(text)
        assert "[密钥]" in out
        assert "ghp_abcdefghijklmnopqrstuvwxyz1234" not in out

    def test_akid_prefix_replaced(self):
        text = "aws key AKIAIOSFODNN7EXAMPLE"
        out = sanitize(text)
        assert "AKIAIOSFODNN7EXAMPLE" not in out

    def test_32_char_token_replaced(self):
        text = "随机令牌 Ab3xY9zQw1EfGh2JkLmNpQrStUvWxYz1234 已生成"
        out = sanitize(text)
        assert "Ab3xY9zQw1EfGh2JkLmNpQrStUvWxYz1234" not in out


class TestContacts:
    def test_email_replaced(self):
        text = "联系 dev.zhang@example.com 获取权限"
        out = sanitize(text)
        assert "[联系方式]" in out
        assert "dev.zhang@example.com" not in out

    def test_phone_replaced(self):
        text = "电话 13812345678 已登记"
        out = sanitize(text)
        assert "[联系方式]" in out
        assert "13812345678" not in out


class TestLongToken:
    def test_long_token_replaced(self):
        long = "A" * 80
        text = f"窗口标题噪音{long}结尾"
        out = sanitize(text)
        assert "[长文本]" in out
        assert "A" * 40 not in out

    def test_short_token_kept(self):
        text = "hello world this is fine"
        assert sanitize(text) == text


class TestChain:
    def test_mixed_sensitive_text_all_replaced(self):
        text = (
            "参考路径 C:\\Users\\admin\\code\\app。\n"
            "代码：def foo():\n"
            "    return create_engine('postgresql://db')\n"
            "密钥 sk-abcdefgh1234567890abcdefgh 已配置。\n"
            "邮箱 a@b.com 电话 13912345678"
        )
        out = sanitize(text)
        assert "[路径]" in out
        assert "[代码片段]" in out
        assert "[密钥]" in out
        assert "[联系方式]" in out
        for sensitive in ("C:\\Users\\admin\\code\\app", "sk-abcdefgh1234567890", "a@b.com", "13912345678"):
            assert sensitive not in out

    def test_empty_input(self):
        assert sanitize("") == ""
        assert sanitize(None if False else "") == ""
