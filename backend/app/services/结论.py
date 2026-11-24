"""对拍结论：核对对齐差、悬账单、日界旁路凭证是否自洽。"""
from __future__ import annotations
from app.db import 连接
from app.seed import 此刻
from app.modules.日界切开禁跨 import 当前策略, 禁跨, 打标, _凭证有效, _差行有效

def 跑一次() -> dict:
    conn = 连接()
    try:
        左 = conn.execute("SELECT COUNT(*) AS c FROM 左轨流水").fetchone()["c"]
        右 = conn.execute("SELECT COUNT(*) AS c FROM 右轨流水").fetchone()["c"]
        对齐 = conn.execute(
            "SELECT COUNT(*) AS c FROM 对齐差 WHERE 差型='对齐'"
        ).fetchone()["c"]
        悬 = conn.execute("SELECT COUNT(*) AS c FROM 悬账单").fetchone()["c"]
        差总 = conn.execute("SELECT COUNT(*) AS c FROM 对齐差").fetchone()["c"]
        跨日差 = conn.execute("SELECT COUNT(*) AS c FROM 对齐差 WHERE 跨日=1").fetchone()["c"]
        凭证行 = conn.execute("SELECT * FROM 日界旁路凭证").fetchall()
        凭证数 = len(凭证行)
        策略 = 当前策略()

        问题 = []
        # 悬账单业务键应能在对齐差找到左缺/右缺，或由日界旁路凭证背书
        for r in conn.execute("SELECT * FROM 悬账单").fetchall():
            行 = conn.execute(
                "SELECT * FROM 对齐差 WHERE 业务键=? ORDER BY 编号", (r["业务键"],)
            ).fetchall()
            票 = conn.execute(
                "SELECT * FROM 日界旁路凭证 WHERE 业务键=? AND 悬边=?",
                (r["业务键"], r["悬边"]),
            ).fetchall()
            if not 行 and not 票:
                问题.append(f"悬账{r['业务键']}无对齐差行亦无旁路凭证")
                continue
            if 行:
                跨日背书 = any(x["跨日"] == 1 for x in 行)
                差型集 = {x["差型"] for x in 行}
                # 跨日打标差行本身即悬账背书（日界切开导致对侧同日缺腿）
                if not 跨日背书:
                    if r["悬边"] == "左" and not (差型集 & {"右缺", "金额不符"}):
                        问题.append(f"左悬{r['业务键']}差型={','.join(sorted(差型集))}")
                    elif r["悬边"] == "右" and not (差型集 & {"左缺", "金额不符"}):
                        问题.append(f"右悬{r['业务键']}差型={','.join(sorted(差型集))}")
            for p in 票:
                if not _凭证有效(conn, p):
                    问题.append(f"旁路凭证{p['凭证号']}金额与流水不符(valid=0)")

        # 每张旁路凭证必须有同键同边悬账背书，且禁跨口径下不得出现跨日对齐差行
        for p in 凭证行:
            背书 = conn.execute(
                "SELECT 1 FROM 悬账单 WHERE 业务键=? AND 悬边=?",
                (p["业务键"], p["悬边"]),
            ).fetchone()
            if not 背书:
                问题.append(f"旁路凭证{p['凭证号']}无悬账背书")
        if 策略 == 禁跨 and 跨日差:
            问题.append(f"禁跨口径下出现{跨日差}行跨日对齐差")
        if 策略 == 打标 and 凭证数:
            问题.append(f"打标口径下残留{凭证数}张禁跨旁路凭证")
        for d in conn.execute("SELECT * FROM 对齐差 WHERE 跨日=1").fetchall():
            if not _差行有效(conn, d):
                问题.append(f"跨日差行{d['业务键']}金额与流水不符(valid=0)")
            if not (d["左业务日"] or d["右业务日"]):
                问题.append(f"跨日差行{d['业务键']}缺业务日标记")

        通过 = len(问题) == 0 and (左 + 右 == 0 or 差总 > 0 or 凭证数 > 0 or (左 > 0 and 右 > 0 and 对齐 >= 0))
        if 左 == 0 and 右 == 0:
            通过 = True
            说明 = "双轨皆空，结论空跑通过"
        elif 问题:
            通过 = False
            说明 = "问题:" + ";".join(问题[:6])
        else:
            说明 = f"左{左}/右{右}/对齐{对齐}/悬{悬}/差行{差总}/跨日差{跨日差}/旁路凭证{凭证数} 自洽"

        conn.execute(
            """INSERT INTO 对拍结论(时刻,左笔数,右笔数,对齐笔数,悬账笔数,是否通过,说明)
               VALUES (?,?,?,?,?,?,?)""",
            (此刻(), 左, 右, 对齐, 悬, 1 if 通过 else 0, 说明),
        )
        conn.commit()
        return {
            "是否通过": 通过,
            "左笔数": 左,
            "右笔数": 右,
            "对齐笔数": 对齐,
            "悬账笔数": 悬,
            "跨日差笔数": 跨日差,
            "旁路凭证数": 凭证数,
            "策略": 策略,
            "说明": 说明,
            "问题": 问题,
        }
    finally:
        conn.close()
