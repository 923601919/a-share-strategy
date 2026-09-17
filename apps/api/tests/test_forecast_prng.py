"""PRNG 黄金向量：与 tianji 的 xoshiro128** 逐位对账。

为什么这个文件必须存在
----------------------
PRNG 是整个预判功能里**最像魔法、也最容易「看起来对」**的一层：
只要它"看起来随机"，上面所有环节——起卦、装卦、断卦、展示——都会正常运转，
没有任何一处会报错。而它一旦与源实现差一位，结论就是：
**同一标的、同一目标日、两边摇出不同的卦**，且这个错误无法从盘面上察觉。

所以判据不能是"分布看起来均匀"，只能是逐位相等：
同一份种子材料 → 同一串 uint32 → 同一组卦。

fixture 由 tianji 自身的 `Prng` + `derivePrng` dump 而成（见 `dump-prng.mjs`），
不是在本项目里重算的——自己算给自己看证明不了任何事。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[1]
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from forecast.prng import Prng  # noqa: E402
from forecast.seed import build_seed_material, derive_prng  # noqa: E402

FIXTURE = json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "forecast_prng_golden.json").read_text("utf-8")
)


def test_seed_material_format():
    """种子材料的字面格式：`符号大写|目标日|盐值`。四元素不能多也不能少。"""
    assert build_seed_material("000001.SH", "2026-09-18", "yz-standard-v1") == (
        "000001.SH|2026-09-18|yz-standard-v1"
    )
    # 小写会被 upper()：同一标的不同大小写必须归一到同一个卦，
    # 否则 "sz399006" 与 "SZ399006" 会摇出两条互不相干的记录。
    assert build_seed_material("sz399006", "2027-02-05", "s") == "SZ399006|2027-02-05|s"


@pytest.mark.parametrize("sample", FIXTURE["samples"], ids=lambda s: f"{s['domain']}@{s['material'][:12]}")
def test_prng_uint32_stream(sample):
    """40 个连续 uint32 必须逐个相等。"""
    _, prng = derive_prng(sample["material"], sample["domain"])
    got = [prng.next_uint32() for _ in range(40)]
    assert got == sample["uint32"]


@pytest.mark.parametrize("sample", FIXTURE["samples"], ids=lambda s: f"{s['domain']}@{s['material'][:12]}")
def test_prng_next_below_no_modulo_bias(sample):
    """拒绝采样的取模路径：不同 n 下的序列必须逐个相等。

    这里覆盖的是「无取模偏差」那一段代码。若把拒绝采样简化成 `x % n`，
    分布仍"看起来均匀"，但序列会变——本用例会立刻抓住。

    ⚠ 每个 n 都用**全新的** prng：fixture 就是这么 dump 的，测试必须同口径。
    在同一实例上连续跑不同 n 会得到错位序列（这是本用例第二版踩的坑，
    和 fixture 第一版踩的是同一个坑的两面）。
    """
    for n_str, want in sample["below"].items():
        n = int(n_str)
        _, prng = derive_prng(sample["material"], sample["domain"])
        got = [prng.next_below(n) for _ in range(len(want))]
        assert got == want, f"next_below({n}) 不一致"


@pytest.mark.parametrize("sample", FIXTURE["samples"], ids=lambda s: f"{s['domain']}@{s['material'][:12]}")
def test_prng_next_below_stays_in_range(sample):
    """返回值必须落在 [0, n) —— 边界本身也要断言，不能只看序列相等。"""
    for n_str, want in sample["below"].items():
        n = int(n_str)
        assert all(0 <= v < n for v in want)


@pytest.mark.parametrize("sample", FIXTURE["samples"], ids=lambda s: f"{s['domain']}@{s['material'][:12]}")
def test_prng_next_float(sample):
    _, prng = derive_prng(sample["material"], sample["domain"])
    got = [prng.next_float() for _ in range(8)]
    assert got == sample["floats"]


@pytest.mark.parametrize("sample", FIXTURE["samples"], ids=lambda s: f"{s['domain']}@{s['material'][:12]}")
def test_prng_interleaved_calls_share_one_state_chain(sample):
    """交叉调用 `next_uint32` / `next_below` / `next_float`，序列必须逐步相等。

    这一段专门抓一类错：**每种取数方法各持一个独立的 prng 实例**。
    那样单看每一段都"对"，但真实的起卦代码是交叉调用的
    （cast_by_coin 每爻连投三次 `next_float`），一旦状态不共享，
    摇出来的卦会与源实现完全不同，而每一段的自测都是绿的。
    """
    _, prng = derive_prng(sample["material"], sample["domain"])
    for i, step in enumerate(sample["pipeline"]):
        if step["op"] == "u32":
            got = prng.next_uint32()
        elif step["op"] == "float":
            got = prng.next_float()
        else:
            got = prng.next_below(step["n"])
        assert got == step["value"], f"第 {i} 步（{step['op']}）状态链发生偏移"


@pytest.mark.parametrize("sample", FIXTURE["samples"], ids=lambda s: f"{s['domain']}@{s['material'][:12]}")
def test_derive_prng_digest(sample):
    """digest 是**种子材料的** sha256（不含 domain），它会写进记录供复核。"""
    digest, _ = derive_prng(sample["material"], sample["domain"])
    assert digest == sample["digest"]


def test_domain_separation_changes_stream():
    """domain 不同则随机流不同，但 digest 相同（digest 只是材料的指纹）。"""
    d_cast, p_cast = derive_prng("000001.SH|2026-09-18|yz-standard-v1", "cast")
    d_bt, p_bt = derive_prng("000001.SH|2026-09-18|yz-standard-v1", "backtest")
    assert d_cast == d_bt
    assert p_cast.next_uint32() != p_bt.next_uint32()


def test_next_below_one_does_not_advance_state():
    """`next_below(1)` 恒为 0 且**不消耗**随机数。

    若不特判 n==1，`0x100000000 // 1 * 1 == 2**32`，`x < limit` 恒成立，
    看似也对；但多消耗一个 uint32 会让后续序列整体错位——
    而"错位"在上层表现为"摇出了另一个卦"，依旧不会报错。
    """
    want = FIXTURE["nextBelowOne"]
    p = Prng(bytes([1, 2, 3, 4, 5, 6, 7, 8]))
    assert p.next_uint32() == want["before"]
    assert [p.next_below(1), p.next_below(1)] == want["results"]
    assert p.next_uint32() == want["after"]


def test_min_legal_seed_is_four_bytes():
    """4 字节是最短合法种子，且只 XOR 第 0 个字。"""
    p = Prng(bytes([0xDE, 0xAD, 0xBE, 0xEF]))
    assert [p.next_uint32() for _ in range(3)] == FIXTURE["minSeed"]


def test_all_zero_seed_is_replaced():
    """xoshiro 不允许全零状态：全零种子必须被替换，否则随机流恒为 0。"""
    p = Prng(bytes([0, 0, 0, 0]))
    got = [p.next_uint32(), p.next_uint32()]
    assert got == FIXTURE["zeroSeed"]
    assert got != [0, 0]


def test_short_seed_raises():
    """种子不足 4 字节必须抛错——**不要**补零凑数：补零会让不同调用方
    在同一个短种子下拿到同一条随机流，而这一点完全看不出来。"""
    with pytest.raises(ValueError) as e:
        Prng(bytes([1, 2, 3]))
    assert "至少需要 4 字节" in str(e.value)


@pytest.mark.parametrize("bad", [0, -1])
def test_next_below_rejects_non_positive(bad):
    p = Prng(bytes([9, 9, 9, 9]))
    with pytest.raises(ValueError):
        p.next_below(bad)
