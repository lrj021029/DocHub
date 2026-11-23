"""标准 RFC 9562 UUID v7 生成器（纯 Python 实现，时间有序且单调递增）。

遵循 ADR-002 决策：
- 在应用层统一生成时间有序的 UUID v7 主键，无需依赖 PostgreSQL 18 或额外 C 扩展。
- 布局符合 RFC 9562 Section 5.7：
  - 48 bit Unix 毫秒时间戳（大端）
  - 4 bit 版本号（0b0111 = 7）
  - 12 bit 单调序列/随机位（rand_a，同毫秒内严格递增，防止排序反转）
  - 2 bit 变体位（0b10，RFC 9562 Variant）
  - 62 bit 密码学安全随机位（rand_b）
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid

_LOCK = threading.Lock()
_LAST_TIMESTAMP_MS = 0
_LAST_SEQUENCE = 0

_MAX_12BIT = 0xFFF
_SEED_MASK_11BIT = 0x7FF  # 同毫秒初始随机种子预留至少 2048 个递增槽位，减少时间戳推进频率


def new_uuid7() -> uuid.UUID:
    """生成一个严格单调递增的 RFC 9562 UUID v7 对象。

    在多线程并发及同一毫秒高频调用场景下，通过进程内互斥锁与 12-bit 序列号递增，
    保证每次返回的 UUID v7 按字典序和整数序严格递增。
    """
    global _LAST_TIMESTAMP_MS, _LAST_SEQUENCE

    with _LOCK:
        now_ms = time.time_ns() // 1_000_000
        if now_ms > _LAST_TIMESTAMP_MS:
            _LAST_TIMESTAMP_MS = now_ms
            _LAST_SEQUENCE = secrets.randbits(11) & _SEED_MASK_11BIT
        else:
            # 同一毫秒或时钟轻微回拨时递增 12 位序列号；溢出则将逻辑毫秒数向前推进 1ms
            _LAST_SEQUENCE += 1
            if _LAST_SEQUENCE > _MAX_12BIT:
                _LAST_TIMESTAMP_MS += 1
                _LAST_SEQUENCE = 0

        ts_ms = _LAST_TIMESTAMP_MS
        rand_a = _LAST_SEQUENCE

    rand_b = secrets.randbits(62)

    # 拼装 128 位 UUID 整数
    value_int = (
        ((ts_ms & 0xFFFFFFFFFFFF) << 80)
        | (0x7 << 76)
        | ((rand_a & _MAX_12BIT) << 64)
        | (0b10 << 62)
        | (rand_b & ((1 << 62) - 1))
    )
    return uuid.UUID(int=value_int)


def new_uuid7_str() -> str:
    """生成标准连字符格式的 UUID v7 字符串。"""
    return str(new_uuid7())


def extract_uuid7_timestamp_ms(value: uuid.UUID) -> int:
    """从 UUID v7 中提取 48 位 Unix 毫秒时间戳。

    Raises:
        ValueError: 当传入 UUID 不是版本 7 时抛出。
    """
    if value.version != 7:
        raise ValueError(f"期望 UUID v7，实际收到版本 {value.version}")
    return value.int >> 80
