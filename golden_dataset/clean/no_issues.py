# 预期：0 问题（测试误报率）
from __future__ import annotations

import hashlib
import os
from typing import Optional


def hash_password(password: str) -> str:
    """使用 PBKDF2 安全地哈希密码。"""
    salt = os.urandom(32)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 100_000)
    return salt.hex() + ":" + key.hex()


def get_user_by_id(db, user_id: int) -> Optional[dict]:
    """安全地查询用户，使用参数化查询。"""
    cursor = db.cursor()
    cursor.execute("SELECT id, name, email FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    if row is None:
        return None
    return {"id": row[0], "name": row[1], "email": row[2]}


def calculate_discount(price: float, rate: float) -> float:
    """计算折扣价格，含边界检查。"""
    if price < 0:
        raise ValueError(f"价格不能为负数：{price}")
    if not (0 <= rate <= 1):
        raise ValueError(f"折扣比例必须在 0-1 之间：{rate}")
    return round(price * (1 - rate), 2)
