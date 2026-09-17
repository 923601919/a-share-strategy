"""确定性伪随机数发生器（xoshiro128**），自 tianji `internal/prng.ts` 逐位移植。

用途只有一个：把「标的 + 目标交易日 + 流派盐值」变成一个固定的卦。
因此要求不是密码学强度，而是：

1. **同种子必然同序列** —— 跨进程、跨设备、跨语言（本移植的关键）都成立；
2. **分布均匀且无取模偏差**；
3. **无平台依赖** —— 不用 `random` 模块。

★ 与 JS 逐位对齐的两个要点 ★

- `Math.imul(a, b)` 是**32 位截断乘法**：JS 取操作数的低 32 位相乘再截断。
  Python 里等价写法是 `(a * b) & 0xFFFFFFFF`（大整数精确相乘后截断低 32 位，
  与 imul 的结果完全一致）。
- `>>> 0` 是无符号右移 0 位，即截断为无符号 32 位 → Python 的 `& 0xFFFFFFFF`。
- `DataView.getUint32(offset)` 默认**大端序**，种子字节按大端读取。

一旦算法改动，历史上所有确定性预测都将无法复现。所以这段代码视同数据表对待：
改动必须提升 ENGINE_VERSION 并清空历史回测。
"""

from __future__ import annotations

MASK = 0xFFFFFFFF


def _rotl(x: int, k: int) -> int:
    return ((x << k) | (x >> (32 - k))) & MASK


class Prng:
    """xoshiro128** + splitmix32 播种。"""

    def __init__(self, seed: bytes) -> None:
        if len(seed) < 4:
            raise ValueError(f"Prng 种子至少需要 4 字节，收到 {len(seed)}")
        acc = int.from_bytes(seed[0:4], "big")
        i = 4
        while i + 4 <= len(seed):
            acc = (acc ^ int.from_bytes(seed[i:i + 4], "big")) & MASK
            i += 4

        mix = _splitmix32(acc)
        self.s0 = mix()
        self.s1 = mix()
        self.s2 = mix()
        self.s3 = mix()
        if (self.s0 | self.s1 | self.s2 | self.s3) == 0:
            # xoshiro 不允许全零状态
            self.s0 = 0x9E3779B9

    def next_uint32(self) -> int:
        result = (_rotl((self.s1 * 5) & MASK, 7) * 9) & MASK
        t = (self.s1 << 9) & MASK
        self.s2 = (self.s2 ^ self.s0) & MASK
        self.s3 = (self.s3 ^ self.s1) & MASK
        self.s1 = (self.s1 ^ self.s2) & MASK
        self.s0 = (self.s0 ^ self.s3) & MASK
        self.s2 = (self.s2 ^ t) & MASK
        self.s3 = _rotl(self.s3, 11)
        return result

    def next_below(self, n: int) -> int:
        """[0, n) 上的均匀整数，无取模偏差（拒绝采样）。"""
        if not isinstance(n, int) or n <= 0:
            raise ValueError(f"nextBelow 需要正整数，收到 {n}")
        if n == 1:
            return 0
        limit = (0x100000000 // n) * n
        for _ in range(64):
            x = self.next_uint32()
            if x < limit:
                return x % n
        raise RuntimeError("PRNG 拒绝采样异常：连续 64 次落空，种子可能已退化")

    def next_float(self) -> float:
        """[0, 1) 上的浮点数。"""
        return self.next_uint32() / 0x100000000


def _splitmix32(seed: int):
    """把单一 32 位种子扩展成质量良好的状态序列。"""
    a = seed & MASK

    def nxt() -> int:
        nonlocal a
        a = (a + 0x9E3779B9) & MASK
        t = a
        t = ((t ^ (t >> 16)) * 0x21F0AAAD) & MASK
        t = ((t ^ (t >> 15)) * 0x735A2D97) & MASK
        return (t ^ (t >> 15)) & MASK

    return nxt
