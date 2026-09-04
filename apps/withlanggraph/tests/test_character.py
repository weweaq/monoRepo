"""Tests for gacore.character: card scanning, name extraction and prompt body loading.

Cards are pure data assets under <asset_dir>/characters/<id>.md:
- id = file stem
- display name = first ``# `` heading line
- prompt body = everything after that heading (what gets injected into the system prompt)
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gacore.character import card_dir, card_name, card_prompt, list_cards
from gacore.config import Config

_CARD_MD_TEMPLATE = """# 楚柒

温柔缱绻，铁骨铮铮。她是你身边的“白月光”助理。
- 性格: 温柔而坚韧，在你面前有几分俏皮
- 请勿自称“我是 AI 助理”
- 单次回复不超过 200 字，多用比喻

今天想聊什么？
"""


@pytest.fixture()
def cfg(tmp_path: Path) -> Config:
    """A hermetic Config rooted at tmp_path with no cards present yet."""
    return Config.for_tests(tmp_path)


def _install_cards(cfg: Config, cards: dict[str, str]) -> None:
    """Seed the characters dir of cfg with the given {id: body} cards."""
    d = card_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    for card_id, body in cards.items():
        (d / f"{card_id}.md").write_text(body, encoding="utf-8")


def test_card_dir_points_under_asset_dir(cfg: Config) -> None:
    assert card_dir(cfg) == cfg.asset_dir / "characters"


def test_list_cards_empty_when_no_dir(cfg: Config) -> None:
    assert list_cards(cfg) == []


def test_list_cards_scans_ids_sorted_and_reads_names(cfg: Config) -> None:
    _install_cards(
        cfg,
        {
            "nami": "# 娜美\n我要画世界地图。\n",
            "inoue": "# 井上织姬\n我一直在守护着。\n",
        },
    )
    cards = list_cards(cfg)
    assert [c.id for c in cards] == ["inoue", "nami"]  # sorted by id
    assert [c.name for c in cards] == ["井上织姬", "娜美"]


def test_list_cards_falls_back_to_stem_when_no_heading(cfg: Config) -> None:
    (card_dir(cfg) / "hanli.md").parent.mkdir(parents=True, exist_ok=True)
    (card_dir(cfg) / "hanli.md").write_text("没有标题，只有正文。\n", encoding="utf-8")
    cards = list_cards(cfg)
    assert len(cards) == 1
    assert cards[0].id == "hanli"
    assert cards[0].name == "hanli"


def test_card_prompt_strips_heading_and_whitespace(cfg: Config) -> None:
    _install_cards(cfg, {"linwan": _CARD_MD_TEMPLATE})
    prompt = card_prompt(cfg, "linwan")
    assert prompt is not None
    assert "温柔缱绻" in prompt
    assert "今天想聊什么？" in prompt
    assert not prompt.startswith("#")  # heading line stripped
    assert prompt == prompt.strip()


def test_card_prompt_is_none_for_missing_card(cfg: Config) -> None:
    assert card_prompt(cfg, "no-such-card") is None


def test_card_name_returns_heading_or_none(cfg: Config) -> None:
    _install_cards(cfg, {"kon": "# 魂\n我是改造魂魄。\n"})
    assert card_name(cfg, "kon") == "魂"
    assert card_name(cfg, "missing") is None
