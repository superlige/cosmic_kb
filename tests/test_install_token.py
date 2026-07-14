"""D4 setup skill 去自举 + D5 版本固定安装口令契约验收。

D4：`cosmic-kb-setup` 不再"负责首次获得 cosmic_kb"，改为从 `install.json` 取 CLI 路径、清单
缺失时明确回退到安装口令。D5：README / 安装说明.md 的 INSTALL-TOKEN 标记对存在且唯一（make_dist
写回的锚点），且内嵌的固定版本与包版本一致（不再人肉维护）。
"""

from __future__ import annotations

from pathlib import Path

from cosmic_kb import __version__
from cosmic_kb.skills import read_skill

REPO_ROOT = Path(__file__).resolve().parents[1]


# ── D4 · setup skill 去自举 ──────────────────────────────────────────────────
def test_setup_skill_drops_self_bootstrap_and_reads_manifest():
    text = read_skill("cosmic-kb-setup").decode("utf-8")
    # 旧自举措辞已删除
    assert "命令不存在时" not in text
    assert "根据当前分发物安装" not in text
    # 改为读 install.json + 清单缺失时回退到安装口令启动 Bootstrap
    assert "install.json" in text
    assert "安装口令" in text
    assert "Bootstrap" in text


# ── D5 · 版本固定安装口令契约 ────────────────────────────────────────────────
def _token_files() -> list[Path]:
    return [REPO_ROOT / "README.md", REPO_ROOT / "scripts" / "安装说明.md"]


def test_install_token_markers_present_exactly_once():
    # make_dist.ps1 靠这对标记做整块替换；多于/少于一对都会写偏。
    for f in _token_files():
        text = f.read_text(encoding="utf-8")
        assert text.count("<!-- INSTALL-TOKEN:START") == 1, f
        assert text.count("<!-- INSTALL-TOKEN:END -->") == 1, f


def test_install_token_pins_current_package_version():
    for f in _token_files():
        text = f.read_text(encoding="utf-8")
        assert f"cosmic-kb=={__version__}" in text, f
        # 口令引导走 bootstrap，且强调口令终端隐藏输入
        assert "bootstrap plan" in text
        assert "bootstrap apply" in text
        assert "prompt-db-password" in text
