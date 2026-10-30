"""The language of the guests module: GUESTS_LANG=en (the default) or ru. One table per
language (texts.py); Russian reproduces the module's original wording byte for byte."""

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import texts  # noqa: E402
import text_scenarios as sc  # noqa: E402

GOLDEN = Path(__file__).resolve().parent / "golden_ru.json"
CYRILLIC = re.compile("[А-Яа-яЁё]")
USER_INPUT = ("Маша", "ну что?")          # what the people in the scenarios typed


def use(monkeypatch, tmp_path, lang=None):
    env = tmp_path / ".env"
    env.write_text("GUESTS_DOMAIN=example.test\n" + (f"GUESTS_LANG={lang}\n" if lang else ""))
    monkeypatch.setenv("GUESTS_ENV_FILE", str(env))


def normalised(obj):
    return json.loads(json.dumps(obj, ensure_ascii=False))


def test_english_is_the_default(monkeypatch, tmp_path):
    use(monkeypatch, tmp_path)
    assert texts.lang() == "en"
    assert texts.t("btn_allow") == "✅ Let in"


@pytest.mark.parametrize("value,expect", [("ru", "ru"), ("RU", "ru"), (" en ", "en"),
                                          ("de", "en"), ("", "en")])
def test_the_setting_picks_the_table(value, expect):
    assert texts.lang({"GUESTS_LANG": value}) == expect


def test_every_key_is_in_both_languages_with_the_same_fields():
    assert set(texts.EN) == set(texts.RU)
    for key in texts.EN:
        assert texts.fields(texts.EN[key]) == texts.fields(texts.RU[key]), key


def test_the_english_table_is_english():
    bad = [k for k, v in texts.EN.items() if CYRILLIC.search(v)]
    assert not bad


def test_russian_reproduces_the_original_texts_byte_for_byte(monkeypatch, tmp_path):
    use(monkeypatch, tmp_path, "ru")
    assert normalised(sc.scenarios()) == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_english_everywhere_a_person_reads(monkeypatch, tmp_path):
    use(monkeypatch, tmp_path, "en")
    text = json.dumps(sc.scenarios(), ensure_ascii=False)
    for typed in USER_INPUT:
        text = text.replace(typed, "")
    assert not CYRILLIC.findall(text), sorted(set(CYRILLIC.findall(text)))


def test_english_bot_samples(monkeypatch, tmp_path):
    use(monkeypatch, tmp_path, "en")
    out = sc.scenarios()
    assert out["buttons"]["inline_keyboard"][0][0]["text"] == "✅ Let in"
    assert out["web_welcome"].startswith("Hello! This is your sandbox with Claude.")
    assert "<site>-<your name>.example.test" in out["web_welcome"]
    assert out["services_full"][0].startswith("Sandbox services (2 of 7 running):")
    assert out["seed_blocks"][2].startswith("\n<!-- sbx-services -->\n## Services")


def test_turnstile_refusals_follow_the_language(monkeypatch, tmp_path):
    import turnstile as ts
    use(monkeypatch, tmp_path, "ru")
    assert texts.t("gate_limit_5h", spent=5, limit=3, at="14:35") == \
        "Лимит на 5 часов исчерпан (5 из 3). Сбросится в 14:35 по Лос-Анджелесу."
    assert texts.t("gate_limit_week", spent=5, limit=3) == \
        "Недельный лимит исчерпан (5 из 3). Сбросится в понедельник в полночь по Лос-Анджелесу."
    assert [texts.t(k) for k in ("gate_bad_pass", "gate_disabled", "gate_path", "gate_upstream")] \
        == ["Неверный пропуск.", "Доступ гостя выключен.", "Этот адрес закрыт.",
            "Сервер Claude недоступен."]
    use(monkeypatch, tmp_path, "en")
    five = texts.t("gate_limit_5h", spent=5, limit=3, at="14:35")
    assert "5-hour limit used up (5 of 3)" in five and "14:35" in five
    week = texts.t("gate_limit_week", spent=5, limit=3)
    assert "Weekly limit used up (5 of 3)" in week and "Monday" in week
    assert "gate_limit" not in texts.EN          # no daily limit any more (30.10.2026)
    assert ts.texts is texts


def test_the_list_lines_show_both_limits_in_english(monkeypatch, tmp_path):
    use(monkeypatch, tmp_path, "en")
    ts_line = texts.t("ts_list_line", name="maria", state="on", five_used=1, five_limit=2,
                      week_used=3, week_limit=14, week_pct=0.01, lifted="", tg=7)
    assert "5h 1 of 2 · week 3 of 14" in ts_line
    ctl_line = texts.t("ctl_list_line", name="maria", tg=7, port=18101, state="running", on="on",
                       five_used=1, five_limit=2, week_used=3, week_limit=14)
    assert "5h 1 of 2 · week 3 of 14" in ctl_line


def test_the_wall_check_labels_follow_the_language(monkeypatch, tmp_path):
    import guestctl as gc
    use(monkeypatch, tmp_path, "ru")
    ru = gc.check_targets()
    assert [label for label, _, _ in ru] == ["турникет Claude", "турникет Телеграм", "ssh сервера",
                                             "nginx сервера", "интернет (example.com:443)"]
    use(monkeypatch, tmp_path, "en")
    assert gc.check_targets()[0][0] == "Claude turnstile"
    assert [t[1:] for t in gc.check_targets()] == [t[1:] for t in ru]
