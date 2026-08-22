"""Tests for the optional semantic embeddings layer (agent/embeddings.py).

numpy is an opt-in dependency (the `voice` extra / LAZY_DEPS, deliberately not
in `[all]`), and agent/embeddings.py imports it lazily so the module degrades to
"no backend" without it. Only the one test that exercises the numpy-backed
cosine math requires it — it importorskips, matching tests/tools/test_voice_mode.py.
Everything else must run on a bare install, so nothing here may import numpy at
module scope.
"""
import pytest

from agent import embeddings as emb


def test_rerank_none_when_no_backend(monkeypatch):
    monkeypatch.setattr(emb, "embed", lambda texts: None)
    assert emb.rerank("q", ["a", "b"], 2) is None


def test_rerank_empty_candidates():
    assert emb.rerank("q", [], 5) == []


def test_rerank_orders_by_cosine(monkeypatch):
    # rerank()'s cosine math is numpy-backed in the module under test, so this
    # is the one case that cannot run without the optional dependency.
    np = pytest.importorskip("numpy", reason="numpy is an optional (voice extra) dep")

    # query vector points at candidate index 1 (the second candidate)
    def fake_embed(texts):
        # texts = [query, cand0, cand1, cand2]
        return np.array([
            [1.0, 0.0],   # query
            [0.0, 1.0],   # cand0 — orthogonal
            [1.0, 0.0],   # cand1 — identical to query (best)
            [0.7, 0.7],   # cand2 — middling
        ], dtype="float32")
    monkeypatch.setattr(emb, "embed", fake_embed)
    order = emb.rerank("q", ["c0", "c1", "c2"], 3)
    assert order[0] == 1            # most similar first
    assert order[-1] == 0          # orthogonal last


def test_hybrid_rerank_falls_back_to_lexical_when_unavailable(monkeypatch):
    monkeypatch.setattr(emb, "rerank", lambda q, c, n: None)
    items = [{"t": "x"}, {"t": "y"}, {"t": "z"}]
    out = emb.hybrid_rerank("q", items, 2, text_of=lambda i: i["t"])
    assert out == items[:2]          # unchanged lexical top-n


def test_hybrid_rerank_reorders_when_available(monkeypatch):
    # rerank says: candidate index 2 is best, then 0
    monkeypatch.setattr(emb, "rerank", lambda q, c, n: [2, 0])
    items = [{"t": "a"}, {"t": "b"}, {"t": "c"}]
    out = emb.hybrid_rerank("q", items, 2, text_of=lambda i: i["t"])
    assert [i["t"] for i in out] == ["c", "a"]


def test_hybrid_rerank_empty():
    assert emb.hybrid_rerank("q", [], 5, text_of=lambda i: i) == []


def test_available_is_bool():
    # Whatever the env, this must not raise and returns a bool.
    assert isinstance(emb.available(), bool)


# --- RRF hybrid fusion (move 7) ---------------------------------------------

def test_reciprocal_rank_fusion_retains_all_and_ranks():
    lexical = [0, 1, 2, 3]
    semantic = [2, 3, 0, 1]
    fused = emb.reciprocal_rank_fusion([lexical, semantic])
    assert set(fused) == {0, 1, 2, 3}       # nothing dropped
    assert fused[0] in (0, 2)               # rank-0 in one list each → top


def test_reciprocal_rank_fusion_item_in_one_list_survives():
    # id 9 appears only in the semantic list, high up → must not be dropped.
    fused = emb.reciprocal_rank_fusion([[0, 1], [9, 0]])
    assert 9 in fused


def test_hybrid_fuse_falls_back_without_backend(monkeypatch):
    monkeypatch.setattr(emb, "rerank", lambda q, c, n: None)  # no backend
    items = [{"t": "x"}, {"t": "y"}, {"t": "z"}]
    assert emb.hybrid_fuse("q", items, 2, text_of=lambda i: i["t"]) == items[:2]


def test_hybrid_fuse_fuses_lexical_and_semantic(monkeypatch):
    # lexical order = [a,b,c]; semantics rank c first. RRF keeps a (lexical rank0)
    # AND lifts c (semantic rank0) — both surface in the top 2.
    monkeypatch.setattr(emb, "rerank", lambda q, c, n: [2, 1, 0])
    items = [{"t": "a"}, {"t": "b"}, {"t": "c"}]
    out = emb.hybrid_fuse("q", items, 2, text_of=lambda i: i["t"])
    assert {i["t"] for i in out} == {"a", "c"}


def test_hybrid_fuse_empty():
    assert emb.hybrid_fuse("q", [], 5, text_of=lambda i: i) == []
