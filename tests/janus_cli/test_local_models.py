"""`janus model --local` — a local model in one command (plan item 3.5)."""
from types import SimpleNamespace

import pytest

from janus_cli import local_models as lm
from janus_cli.config import load_config


def _hw(ram=16, apple=False, vram=None, os_name="darwin"):
    return {"os": os_name, "arch": "arm64" if apple else "x86_64", "ram_gb": ram,
            "apple_silicon": apple, "vram_gb": vram, "gpu": "nvidia" if vram else None}


def test_usable_memory_follows_the_hardware():
    assert lm.usable_memory_gb(_hw(ram=32, apple=True)) > lm.usable_memory_gb(_hw(ram=32))
    assert lm.usable_memory_gb(_hw(ram=64, vram=12)) <= 12


def test_the_largest_model_that_fits_is_chosen():
    small = lm.pick_model(_hw(ram=8))
    big = lm.pick_model(_hw(ram=64, apple=True))
    assert small and big
    assert big["min_mem_gb"] >= small["min_mem_gb"]
    assert lm.pick_model(_hw(ram=1)) is None
    for hw in (_hw(ram=8), _hw(ram=16, apple=True), _hw(ram=64, vram=24)):
        choice = lm.pick_model(hw)
        assert choice["min_mem_gb"] <= lm.usable_memory_gb(hw)


@pytest.fixture()
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(lm, "_run", lambda argv, **k: seen.append(list(argv)) or 0)
    monkeypatch.setattr(lm, "detect_hardware", lambda: _hw(ram=16, apple=True))
    return seen


def _args(**kw):
    base = dict(local=True, dry_run=False, local_model=None, install=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_with_ollama_the_model_is_pulled_and_janus_points_at_it(calls, monkeypatch):
    monkeypatch.setattr(lm, "_which", lambda name: f"/usr/bin/{name}" if name == "ollama" else None)
    monkeypatch.setattr(lm, "_ollama_reachable", lambda: True)
    assert lm.run_local(_args()) == 0
    tag = lm.pick_model(_hw(ram=16, apple=True))["ollama"]
    assert ["ollama", "pull", tag] in calls
    model = load_config()["model"]
    assert model["provider"] == "custom"
    assert model["base_url"] == lm.OLLAMA_BASE_URL
    assert model["default"] == tag
    saved = [p for p in load_config().get("custom_providers") or []
             if p.get("base_url", "").rstrip("/") == lm.OLLAMA_BASE_URL.rstrip("/")]
    assert saved and saved[0]["model"] == tag


def test_dry_run_changes_nothing(calls, monkeypatch, capsys):
    monkeypatch.setattr(lm, "_which", lambda name: "/usr/bin/ollama" if name == "ollama" else None)
    before = load_config().get("model")
    assert lm.run_local(_args(dry_run=True)) == 0
    assert calls == []
    assert load_config().get("model") == before
    assert "ollama pull" in capsys.readouterr().out


def test_no_backend_explains_how_to_install(calls, monkeypatch, capsys):
    monkeypatch.setattr(lm, "_which", lambda name: None)
    assert lm.run_local(_args()) == 1
    out = capsys.readouterr().out
    assert "ollama" in out.lower() and "--install" in out
    assert calls == []


def test_install_uses_homebrew_on_macos_then_continues(calls, monkeypatch):
    installed = {"ollama": False}
    monkeypatch.setattr(lm, "_which", lambda name: "/opt/homebrew/bin/brew" if name == "brew"
                        else ("/opt/homebrew/bin/ollama" if installed["ollama"] and name == "ollama" else None))
    monkeypatch.setattr(lm, "_ollama_reachable", lambda: True)

    def _run(argv, **k):
        calls.append(list(argv))
        if argv[:3] == ["brew", "install", "ollama"]:
            installed["ollama"] = True
        return 0
    monkeypatch.setattr(lm, "_run", _run)
    assert lm.run_local(_args(install=True)) == 0
    assert calls[0] == ["brew", "install", "ollama"]
    assert any(c[:2] == ["ollama", "pull"] for c in calls)


def test_llama_cpp_only_registers_the_endpoint_and_prints_the_server_command(calls, monkeypatch, capsys):
    monkeypatch.setattr(lm, "_which", lambda name: "/usr/bin/llama-server" if name == "llama-server" else None)
    assert lm.run_local(_args()) == 0
    assert calls == []          # llama-server downloads the GGUF itself when started
    model = load_config()["model"]
    assert model["base_url"] == lm.LLAMA_CPP_BASE_URL
    assert "llama-server -hf" in capsys.readouterr().out


def test_explicit_model_overrides_the_pick(calls, monkeypatch):
    monkeypatch.setattr(lm, "_which", lambda name: "/usr/bin/ollama" if name == "ollama" else None)
    monkeypatch.setattr(lm, "_ollama_reachable", lambda: True)
    assert lm.run_local(_args(local_model="mistral:7b")) == 0
    assert ["ollama", "pull", "mistral:7b"] in calls
    assert load_config()["model"]["default"] == "mistral:7b"


def test_cli_flag_is_wired():
    import inspect
    from janus_cli import main
    assert '"--local"' in inspect.getsource(main)
