"""同业务键对齐：双边金额比对，落对齐差 + 悬账单。"""
from __future__ import annotations
import uuid
from fastapi import HTTPException
from app.db import 连接
from app.seed import 此刻
from app.services import 目录
from app.modules.日界切开禁跨 import 取锁, 放锁, 锁键, 日界冲突

def 跑对齐() -> dict:
    规 = 目录.轨规()
    容差 = int(规.get("金额容差分", "0"))
    持有者 = f"基线-{uuid.uuid4().hex[:8]}"
    时刻 = 此刻()
    conn = 连接()
    try:
        try:
            取锁(conn, 持有者)
        except 日界冲突 as e:
            # 切日与对齐并发不得双落：抢锁失败直接拒绝，不写任何结果
            raise HTTPException(status_code=409, detail=str(e))
        try:
            # 清空上次对齐结果（基线默认：每次重算）
            conn.execute("DELETE FROM 对齐差")
            conn.execute("DELETE FROM 悬账单")
            conn.execute("DELETE FROM 日界旁路凭证")

            # 同键多笔：按业务键汇总金额（基线口径）
            左汇总: dict[str, int] = {}
            左代表: dict[str, str] = {}
            for r in conn.execute("SELECT * FROM 左轨流水 WHERE 命运='已入左'").fetchall():
                k = r["业务键"]
                左汇总[k] = 左汇总.get(k, 0) + int(r["金额分"])
                左代表[k] = r["事件编号"]

            右汇总: dict[str, int] = {}
            右代表: dict[str, str] = {}
            for r in conn.execute("SELECT * FROM 右轨流水 WHERE 命运='已入右'").fetchall():
                k = r["业务键"]
                右汇总[k] = 右汇总.get(k, 0) + int(r["金额分"])
                右代表[k] = r["事件编号"]

            所有键 = set(左汇总) | set(右汇总)
            对齐 = 0
            差 = 0
            悬 = 0

            for k in sorted(所有键):
                有左 = k in 左汇总
                有右 = k in 右汇总
                if 有左 and 有右:
                    la, ra = 左汇总[k], 右汇总[k]
                    if abs(la - ra) <= 容差:
                        conn.execute(
                            "INSERT INTO 对齐差(业务键,左金额分,右金额分,差型,说明,时刻) VALUES (?,?,?,?,?,?)",
                            (k, la, ra, "对齐", "双边金额在容差内", 时刻),
                        )
                        对齐 += 1
                    else:
                        conn.execute(
                            "INSERT INTO 对齐差(业务键,左金额分,右金额分,差型,说明,时刻) VALUES (?,?,?,?,?,?)",
                            (k, la, ra, "金额不符", f"左{la}-右{ra}", 时刻),
                        )
                        差 += 1
                elif 有左:
                    conn.execute(
                        "INSERT INTO 对齐差(业务键,左金额分,右金额分,差型,说明,时刻) VALUES (?,?,?,?,?,?)",
                        (k, 左汇总[k], None, "右缺", "仅左轨有键", 时刻),
                    )
                    conn.execute(
                        "INSERT INTO 悬账单(业务键,悬边,金额分,事件编号,原因,时刻) VALUES (?,?,?,?,?,?)",
                        (k, "左", 左汇总[k], 左代表.get(k, ""), "右轨缺键", 时刻),
                    )
                    悬 += 1
                    差 += 1
                else:
                    conn.execute(
                        "INSERT INTO 对齐差(业务键,左金额分,右金额分,差型,说明,时刻) VALUES (?,?,?,?,?,?)",
                        (k, None, 右汇总[k], "左缺", "仅右轨有键", 时刻),
                    )
                    conn.execute(
                        "INSERT INTO 悬账单(业务键,悬边,金额分,事件编号,原因,时刻) VALUES (?,?,?,?,?,?)",
                        (k, "右", 右汇总[k], 右代表.get(k, ""), "左轨缺键", 时刻),
                    )
                    悬 += 1
                    差 += 1

            conn.commit()
            return {
                "键总数": len(所有键),
                "对齐": 对齐,
                "差与悬": 差,
                "悬账": 悬,
                "容差分": 容差,
                "锁": 锁键,
                "持有者": 持有者,
                "时刻": 时刻,
            }
        finally:
            放锁(conn, 持有者)
    finally:
        conn.close()
