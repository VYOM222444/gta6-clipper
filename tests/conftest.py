import pytest


@pytest.fixture(autouse=True)
def _baseline_cache_in_tmp(tmp_path, monkeypatch):
    """Never let tests write the real data/channel_baselines.json."""
    import lib.baselines
    monkeypatch.setattr(lib.baselines, "PATH", tmp_path / "channel_baselines.json")
