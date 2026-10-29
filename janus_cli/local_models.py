"""``janus model --local`` — a local model in one command (plan item 3.5).

1. Detect the hardware: RAM, Apple Silicon (unified memory), NVIDIA VRAM.
2. Pick the largest model from a small curated table that fits the memory a
   model can realistically use on that machine.
3. With Ollama installed: ``ollama pull`` it and point Janus at Ollama's
   OpenAI-compatible endpoint. With only llama.cpp's ``llama-server``:
   register its endpoint and print the exact command that downloads and
   serves the GGUF (``llama-server -hf …``).
4. Nothing is installed unless ``--install`` is given (Homebrew on macOS);
   ``--dry-run`` shows the plan and changes nothing.

No API key is needed afterwards; the model is saved as the default custom
provider and to ``custom_providers`` so it shows up in ``janus model``.
"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

OLLAMA_BASE_URL = "http://localhost:11434/v1"
LLAMA_CPP_BASE_URL = "http://localhost:8080/v1"

# Largest first. min_mem_gb is the memory the model needs to run comfortably
# at its default quantisation (weights + KV cache for a modest context).
CATALOG: List[Dict[str, Any]] = [
    {"name": "Qwen3 32B", "ollama": "qwen3:32b", "gguf": "Qwen/Qwen3-32B-GGUF", "min_mem_gb": 24},
    {"name": "Qwen3 14B", "ollama": "qwen3:14b", "gguf": "Qwen/Qwen3-14B-GGUF", "min_mem_gb": 12},
    {"name": "Qwen3 8B", "ollama": "qwen3:8b", "gguf": "Qwen/Qwen3-8B-GGUF", "min_mem_gb": 7},
    {"name": "Qwen3 4B", "ollama": "qwen3:4b", "gguf": "Qwen/Qwen3-4B-GGUF", "min_mem_gb": 4},
    {"name": "Qwen3 1.7B", "ollama": "qwen3:1.7b", "gguf": "Qwen/Qwen3-1.7B-GGUF", "min_mem_gb": 2},
]


# -- probes (patched in tests) -----------------------------------------------

def _which(name: str) -> Optional[str]:
    return shutil.which(name)


def _run(argv: List[str], **kwargs) -> int:
    return subprocess.call(argv, **kwargs)


def _ollama_reachable() -> bool:
    try:
        import urllib.request
        with urllib.request.urlopen(OLLAMA_BASE_URL.rsplit("/v1", 1)[0] + "/api/tags", timeout=2):
            return True
    except Exception:
        return False


def _ram_gb() -> float:
    try:
        import psutil
        return psutil.virtual_memory().total / 1024 ** 3
    except Exception:
        pass
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5)
            return int(out.stdout.strip()) / 1024 ** 3
        pages, size = os.sysconf("SC_PHYS_PAGES"), os.sysconf("SC_PAGE_SIZE")
        return pages * size / 1024 ** 3
    except Exception:
        return 0.0


def _nvidia_vram_gb() -> Optional[float]:
    if not _which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10)
        values = [float(v) for v in out.stdout.split() if v.strip()]
        return max(values) / 1024 if values else None
    except Exception:
        return None


def detect_hardware() -> Dict[str, Any]:
    arch = platform.machine().lower()
    apple = sys.platform == "darwin" and arch in {"arm64", "aarch64"}
    vram = None if apple else _nvidia_vram_gb()
    return {"os": sys.platform, "arch": arch, "ram_gb": round(_ram_gb(), 1),
            "apple_silicon": apple, "vram_gb": round(vram, 1) if vram else None,
            "gpu": "apple" if apple else ("nvidia" if vram else None)}


# -- choice ------------------------------------------------------------------

def usable_memory_gb(hw: Dict[str, Any]) -> float:
    """Memory a local model can realistically use on this machine."""
    ram = float(hw.get("ram_gb") or 0)
    if hw.get("apple_silicon"):
        return ram * 0.70          # unified memory; leave room for the OS and apps
    vram = hw.get("vram_gb")
    if vram:
        return float(vram) * 0.90  # weights must fit on the GPU to be fast
    return ram * 0.50              # CPU inference from system RAM


def pick_model(hw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    budget = usable_memory_gb(hw)
    for entry in CATALOG:
        if entry["min_mem_gb"] <= budget:
            return entry
    return None


# -- config ------------------------------------------------------------------

def _save(base_url: str, model: str, name: str) -> None:
    from janus_cli.config import load_config, save_config
    cfg = load_config()
    current = cfg.get("model")
    model_cfg = dict(current) if isinstance(current, dict) else ({"default": current} if current else {})
    model_cfg.update({"provider": "custom", "base_url": base_url, "default": model})
    model_cfg.pop("api_mode", None)
    model_cfg.pop("api_key", None)
    cfg["model"] = model_cfg
    providers = [p for p in (cfg.get("custom_providers") or []) if isinstance(p, dict)]
    for entry in providers:
        if str(entry.get("base_url", "")).rstrip("/") == base_url.rstrip("/"):
            entry["model"] = model
            entry.setdefault("name", name)
            break
    else:
        providers.append({"name": name, "base_url": base_url, "model": model})
    cfg["custom_providers"] = providers
    save_config(cfg)
    try:
        from janus_cli.auth import deactivate_provider
        deactivate_provider()
    except Exception:
        pass


# -- flow --------------------------------------------------------------------

def _install_hint(hw: Dict[str, Any]) -> str:
    if hw.get("os") == "darwin":
        return "brew install ollama   (or download it from https://ollama.com/download)"
    if str(hw.get("os", "")).startswith("linux"):
        return "see https://ollama.com/download/linux for the official installer"
    return "download Ollama from https://ollama.com/download"


def run_local(args) -> int:
    hw = detect_hardware()
    explicit = (getattr(args, "local_model", None) or "").strip() or None
    choice = pick_model(hw)
    dry = bool(getattr(args, "dry_run", False))

    print(f"\n  Hardware: {hw.get('ram_gb')} GB RAM"
          + (" · Apple Silicon (unified memory)" if hw.get("apple_silicon") else "")
          + (f" · NVIDIA {hw.get('vram_gb')} GB VRAM" if hw.get("vram_gb") else "")
          + f" · ~{usable_memory_gb(hw):.0f} GB usable for a model")
    if not explicit and choice is None:
        print("  ✗ This machine does not have enough memory for any model in the local catalog.")
        return 1
    if choice and not explicit:
        print(f"  Model:    {choice['name']} (needs ~{choice['min_mem_gb']} GB)")

    has_ollama = bool(_which("ollama"))
    has_llama = bool(_which("llama-server"))
    if not has_ollama and not has_llama and getattr(args, "install", False):
        if hw.get("os") == "darwin" and _which("brew"):
            print("  Installing Ollama with Homebrew …")
            if dry:
                print("    would run: brew install ollama")
            elif _run(["brew", "install", "ollama"]) != 0:
                print("  ✗ brew install ollama failed.")
                return 1
            has_ollama = dry or bool(_which("ollama"))
        else:
            print(f"  Automatic install is only supported with Homebrew on macOS; {_install_hint(hw)}.")
            return 1

    if has_ollama:
        tag = explicit or choice["ollama"]
        print(f"  Backend:  Ollama → {OLLAMA_BASE_URL}")
        if dry:
            print(f"\n  Would run: ollama pull {tag}\n  Would set the default model to {tag} via Ollama.\n")
            return 0
        if not _ollama_reachable():
            print("  Ollama is installed but not running; start it with `ollama serve` "
                  "(or open the Ollama app), then rerun.")
            return 1
        print(f"\n  Pulling {tag} (first time can take a while) …\n")
        if _run(["ollama", "pull", tag]) != 0:
            print(f"  ✗ ollama pull {tag} failed.")
            return 1
        _save(OLLAMA_BASE_URL, tag, "Ollama (local)")
        print(f"\n  ✓ Default model is now {tag} via Ollama. No API key needed.\n")
        return 0

    if has_llama:
        repo = explicit or choice["gguf"]
        command = f"llama-server -hf {repo} --port 8080 --jinja"
        print(f"  Backend:  llama.cpp → {LLAMA_CPP_BASE_URL}")
        if not dry:
            _save(LLAMA_CPP_BASE_URL, repo, "llama.cpp (local)")
        print(f"\n  {'Would set' if dry else '✓ Set'} the default model to {repo} via llama.cpp.")
        print(f"  Start the server (it downloads the model the first time):\n\n    {command}\n")
        return 0

    print(f"\n  No local model runtime found. Install Ollama: {_install_hint(hw)}\n"
          "  or rerun with --install (Homebrew on macOS), or install llama.cpp's llama-server.\n")
    return 1
