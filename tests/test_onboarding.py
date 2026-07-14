from streamlit.testing.v1 import AppTest

from core import db


def _script():
    from views import onboarding
    onboarding.render()


def _widget(seq, key):
    return next(w for w in seq if w.key == key)


def test_backfill_marks_existing_db_onboarded(tmp_db):
    """Bestehende Installation (mit Positionen) wird beim init_db als onboarded markiert."""
    assert db.get_meta("onboarded") is None          # frische Test-DB
    tmp_db.save_position("NVDA", "stock", 10.0, 100.0, name="NVDA")
    tmp_db.init_db()                                 # erneuter Start
    assert db.get_meta("onboarded") is not None


def test_onboarding_renders_and_skip_sets_flag(tmp_db):
    at = AppTest.from_function(_script).run(timeout=30)
    assert not at.exception
    assert db.get_meta("onboarded") is None
    _widget(at.button, "onboard_skip").click().run(timeout=30)
    assert db.get_meta("onboarded") is not None


def test_onboarding_finish_saves_key_and_profile(tmp_db):
    at = AppTest.from_function(_script).run(timeout=30)
    _widget(at.text_input, "onboard_anthropic").set_value("sk-test-123")
    at.run(timeout=30)
    _widget(at.button, "onboard_finish").click().run(timeout=30)
    assert db.get_meta("anthropic_api_key") == "sk-test-123"
    assert db.get_meta("target_allocation") is not None
    assert db.get_meta("risk_profile") is not None
    assert db.get_meta("onboarded") is not None
