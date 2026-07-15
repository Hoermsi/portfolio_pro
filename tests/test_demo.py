"""Tests für den Demo-Modus (separate DB, Seed-Depot, synthetischer Verlauf)."""
from core import config, demo


def test_set_demo_mode_switches_db(monkeypatch):
    """set_demo_mode schaltet die aktive DB zwischen echt und Demo um."""
    monkeypatch.setattr(config, "DB_PATH", config.REAL_DB_PATH)
    config.set_demo_mode(True)
    assert config.DB_PATH == config.DEMO_DB_PATH
    config.set_demo_mode(False)
    assert config.DB_PATH == config.REAL_DB_PATH


def test_ensure_seeded_populates(tmp_db):
    demo.ensure_seeded()
    assert tmp_db.get_meta("demo_seeded")
    assert tmp_db.get_meta("onboarded")            # überspringt Ersteinrichtung
    assert len(tmp_db.list_positions("stock")) == len(demo.SEED_STOCKS)
    assert len(tmp_db.list_positions("crypto")) == len(demo.SEED_CRYPTO)
    assert tmp_db.latest_cash_balance() == demo.SEED_CASH
    # ~540 Tage × 3 Anlageklassen synthetische Snapshots
    assert len(tmp_db.list_snapshots()) >= 1500


def test_ensure_seeded_idempotent(tmp_db):
    demo.ensure_seeded()
    n_pos = len(tmp_db.list_positions())
    n_snap = len(tmp_db.list_snapshots())
    demo.ensure_seeded()  # zweiter Aufruf darf nichts verdoppeln
    assert len(tmp_db.list_positions()) == n_pos
    assert len(tmp_db.list_snapshots()) == n_snap
    assert n_pos == len(demo.SEED_STOCKS) + len(demo.SEED_CRYPTO)


def test_demo_history_is_monotonic_and_positive(tmp_db):
    from analysis import performance
    demo.ensure_seeded()
    hist = performance.history_df()
    assert hist is not None
    assert len(hist) >= 500
    assert list(hist.index) == sorted(hist.index)   # aufsteigende Datumsreihe
    assert (hist["Gesamt"] > 0).all()
