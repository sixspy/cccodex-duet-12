"""日界切开禁跨（邻题主链路，独立于双轨对齐 happy path）。

问题：跨业务日的左右同键会被基线按业务键压成一笔，或同日事件被日界误切。
本模块把流水按「业务键 + 业务日（事件时 UTC 日期）」重新归组后再编排对齐：

* 同日双边：照旧落对齐差（差行带左/右业务日，跨日=0）。
* 跨日同键二择一（轨规键 日界跨日策略）：
  - 禁跨（默认）：不写双边对齐差；两侧各写一张「日界旁路凭证」钉住跨日，
    结论改读旁路集（悬账单由旁路凭证背书）。
  - 打标：仍写对齐差并置跨日=1、记录左右业务日，不写禁跨旁路。
* 切日编排与基线对齐共用一把对齐锁，并发只准一个落库，绝不双落。
"""
from __future__ import annotations
import uuid
import sqlite3
from datetime import timezone

from app.db import 连接
from app.seed import 此刻
from app.services import 目录
from app.services.时间 import 解析

NAME = "日界切开禁跨"

锁键 = "对齐编排"
策略键 = "日界跨日策略"
禁跨 = "禁跨"
打标 = "打标"
旁路差型 = "禁跨旁路"


class 日界冲突(Exception):
    """对齐编排锁已被占用（切日/对齐并发，禁止双落）。"""


def 当前策略() -> str:
    v = str(目录.轨规().get(策略键, 禁跨) or 禁跨).strip()
    return 打标 if v == 打标 else 禁跨


def 改策略(值: str) -> dict:
    if 值 not in (禁跨, 打标):
        raise ValueError(f"未知日界策略:{值}")
    目录.改轨规({策略键: 值})
    return {"策略键": 策略键, "策略": 值}


def 取锁(conn: sqlite3.Connection, 持有者: str) -> None:
    try:
        conn.execute(
            "INSERT INTO 对齐锁(锁键,持有者,取得时) VALUES (?,?,?)",
            (锁键, 持有者, 此刻()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        行 = conn.execute("SELECT 持有者 FROM 对齐锁 WHERE 锁键=?", (锁键,)).fetchone()
        raise 日界冲突(f"对齐编排锁被占用:{行['持有者'] if 行 else '?'}")


def 放锁(conn: sqlite3.Connection, 持有者: str) -> None:
    conn.execute("DELETE FROM 对齐锁 WHERE 锁键=? AND 持有者=?", (锁键, 持有者))
    conn.commit()


def _业务日(事件时: str) -> str:
    return 解析(事件时).astimezone(timezone.utc).strftime("%Y-%m-%d")


def _归组(conn: sqlite3.Connection, 表: str, 命运: str) -> dict:
    组: dict[tuple[str, str], dict] = {}
    for r in conn.execute(f"SELECT * FROM {表} WHERE 命运=?", (命运,)).fetchall():
        键, 日 = r["业务键"], _业务日(r["事件时"])
        g = 组.setdefault((键, 日), {"额": 0, "代表": r["事件编号"]})
        g["额"] += int(r["金额分"])
    return 组


def _写差(conn, 键, 左额, 右额, 差型, 说明, 左日, 右日, 跨日, 时刻):
    conn.execute(
        """INSERT INTO 对齐差(业务键,左金额分,右金额分,差型,说明,时刻,左业务日,右业务日,跨日)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (键, 左额, 右额, 差型, 说明, 时刻, 左日, 右日, 1 if 跨日 else 0),
    )


def 跑日界对齐() -> dict:
    """按业务日切开的对齐编排。持锁全量重算，与基线跑对齐互斥。"""
    规 = 目录.轨规()
    容差 = int(规.get("金额容差分", "0"))
    策略 = 当前策略()
    持有者 = f"日界-{uuid.uuid4().hex[:8]}"
    时刻 = 此刻()
    conn = 连接()
    try:
        取锁(conn, 持有者)
        try:
            # 全量重算：日界编排与基线对齐共用结果表，后跑者整体覆盖
            conn.execute("DELETE FROM 对齐差")
            conn.execute("DELETE FROM 悬账单")
            conn.execute("DELETE FROM 日界旁路凭证")

            左组 = _归组(conn, "左轨流水", "已入左")
            右组 = _归组(conn, "右轨流水", "已入右")
            左日表: dict[str, set[str]] = {}
            右日表: dict[str, set[str]] = {}
            for (k, d) in 左组:
                左日表.setdefault(k, set()).add(d)
            for (k, d) in 右组:
                右日表.setdefault(k, set()).add(d)

            同日对齐 = 同日差额 = 跨日键数 = 凭证数 = 悬账数 = 0

            for k in sorted(set(左日表) | set(右日表)):
                左日集 = 左日表.get(k, set())
                右日集 = 右日表.get(k, set())
                共日 = sorted(左日集 & 右日集)

                # 1) 同日双边对齐（含同日单边缺腿）
                for d in 共日:
                    lg, rg = 左组[(k, d)], 右组[(k, d)]
                    la, ra = lg["额"], rg["额"]
                    if abs(la - ra) <= 容差:
                        _写差(conn, k, la, ra, "对齐", "同日双边金额在容差内", d, d, False, 时刻)
                        同日对齐 += 1
                    else:
                        _写差(conn, k, la, ra, "金额不符", f"同日左{la}-右{ra}", d, d, False, 时刻)
                        同日差额 += 1

                # 2) 跨日暴露：左有右无的业务日 / 右有左无的业务日
                左悬日 = sorted(左日集 - 右日集)
                右悬日 = sorted(右日集 - 左日集)
                if not 左悬日 and not 右悬日:
                    continue
                跨日键数 += 1
                左额 = sum(左组[(k, d)]["额"] for d in 左悬日)
                右额 = sum(右组[(k, d)]["额"] for d in 右悬日)
                左日串, 右日串 = ",".join(左悬日), ",".join(右悬日)
                左代表 = 左组[(k, 左悬日[0])]["代表"] if 左悬日 else ""
                右代表 = 右组[(k, 右悬日[0])]["代表"] if 右悬日 else ""

                if 策略 == 禁跨:
                    # 禁跨：不写双边对齐差，两侧旁路凭证钉跨日，结论改读旁路集
                    for 悬边, 日串, 额, 代表 in (
                        ("左", 左日串, 左额, 左代表),
                        ("右", 右日串, 右额, 右代表),
                    ):
                        if not 日串:
                            continue
                        凭证号 = f"BP-{uuid.uuid4().hex[:12]}"
                        conn.execute(
                            """INSERT INTO 日界旁路凭证
                               (凭证号,业务键,悬边,左业务日,右业务日,左金额分,右金额分,差型,原因,时刻)
                               VALUES (?,?,?,?,?,?,?,?,?,?)""",
                            (凭证号, k, 悬边, 左日串, 右日串,
                             左额, 右额, 旁路差型,
                             f"日界禁跨:{悬边}侧业务日{日串}未在对侧同日落账", 时刻),
                        )
                        凭证数 += 1
                        conn.execute(
                            "INSERT INTO 悬账单(业务键,悬边,金额分,事件编号,原因,时刻) VALUES (?,?,?,?,?,?)",
                            (k, 悬边, 额, 代表, f"日界禁跨旁路:{日串}", 时刻),
                        )
                        悬账数 += 1
                else:
                    # 打标：仍写对齐差并标跨日，不写禁跨旁路；读引用差行
                    if 左悬日 and 右悬日:
                        差型 = "对齐" if abs(左额 - 右额) <= 容差 else "金额不符"
                        说明 = f"跨日打标:左日{左日串}/右日{右日串}"
                    elif 左悬日:
                        差型, 说明 = "右缺", f"跨日打标:左日{左日串}右轨同日缺"
                    else:
                        差型, 说明 = "左缺", f"跨日打标:右日{右日串}左轨同日缺"
                    _写差(conn, k,
                          左额 if 左悬日 else None,
                          右额 if 右悬日 else None,
                          差型, 说明, 左日串, 右日串, True, 时刻)
                    if 左悬日:
                        conn.execute(
                            "INSERT INTO 悬账单(业务键,悬边,金额分,事件编号,原因,时刻) VALUES (?,?,?,?,?,?)",
                            (k, "左", 左额, 左代表, f"跨日打标右缺:{左日串}", 时刻),
                        )
                        悬账数 += 1
                    if 右悬日:
                        conn.execute(
                            "INSERT INTO 悬账单(业务键,悬边,金额分,事件编号,原因,时刻) VALUES (?,?,?,?,?,?)",
                            (k, "右", 右额, 右代表, f"跨日打标左缺:{右日串}", 时刻),
                        )
                        悬账数 += 1

            conn.commit()
            return {
                "策略": 策略,
                "键总数": len(set(左日表) | set(右日表)),
                "同日对齐": 同日对齐,
                "同日差额": 同日差额,
                "跨日键": 跨日键数,
                "旁路凭证": 凭证数,
                "悬账": 悬账数,
                "锁": 锁键,
                "持有者": 持有者,
                "时刻": 时刻,
            }
        finally:
            放锁(conn, 持有者)
    finally:
        conn.close()


def _侧现额(conn, 表: str, 键: str, 日集: list[str]) -> int | None:
    if not 日集:
        return None
    q = ",".join("?" for _ in 日集)
    行 = conn.execute(
        f"""SELECT COALESCE(SUM(金额分),0) AS s FROM {表}
            WHERE 业务键=? AND substr(事件时,1,10) IN ({q})""",
        (键, *日集),
    ).fetchone()
    return int(行["s"])


def _拆日(s: str) -> list[str]:
    return [x for x in (s or "").split(",") if x]


def _差行有效(conn, r: sqlite3.Row) -> bool:
    键 = r["业务键"]
    左日, 右日 = _拆日(r["左业务日"]), _拆日(r["右业务日"])
    if 左日 and _侧现额(conn, "左轨流水", 键, 左日) != (r["左金额分"] if r["左金额分"] is not None else 0):
        return False
    if 右日 and _侧现额(conn, "右轨流水", 键, 右日) != (r["右金额分"] if r["右金额分"] is not None else 0):
        return False
    return True


def _凭证有效(conn, r: sqlite3.Row) -> bool:
    键 = r["业务键"]
    左日, 右日 = _拆日(r["左业务日"]), _拆日(r["右业务日"])
    if 左日 and _侧现额(conn, "左轨流水", 键, 左日) != int(r["左金额分"]):
        return False
    if 右日 and _侧现额(conn, "右轨流水", 键, 右日) != int(r["右金额分"]):
        return False
    return True


def 日界列表() -> list[dict]:
    """日界只读接口：差或旁路统一视图，字段含业务键/左日/右日/差或旁路/valid。"""
    conn = 连接()
    try:
        out: list[dict] = []
        for r in conn.execute("SELECT * FROM 对齐差 ORDER BY 业务键, 编号").fetchall():
            out.append({
                "业务键": r["业务键"],
                "左日": r["左业务日"],
                "右日": r["右业务日"],
                "差或旁路": r["差型"],
                "跨日": int(r["跨日"]),
                "左金额分": r["左金额分"],
                "右金额分": r["右金额分"],
                "凭证号": "",
                "说明": r["说明"],
                "时刻": r["时刻"],
                "valid": _差行有效(conn, r),
            })
        for r in conn.execute("SELECT * FROM 日界旁路凭证 ORDER BY 业务键, 凭证号").fetchall():
            out.append({
                "业务键": r["业务键"],
                "悬边": r["悬边"],
                "左日": r["左业务日"],
                "右日": r["右业务日"],
                "差或旁路": 旁路差型,
                "跨日": 1,
                "左金额分": int(r["左金额分"]) or None,
                "右金额分": int(r["右金额分"]) or None,
                "凭证号": r["凭证号"],
                "说明": r["原因"],
                "时刻": r["时刻"],
                "valid": _凭证有效(conn, r),
            })
        return out
    finally:
        conn.close()


def 日界单键(键: str) -> dict:
    """单业务键只读视图：可区分跨日同键是否存在对齐差行 vs 仅旁路凭证。"""
    conn = 连接()
    try:
        差行 = [dict(r) for r in conn.execute(
            "SELECT * FROM 对齐差 WHERE 业务键=? ORDER BY 编号", (键,)).fetchall()]
        凭证 = [dict(r) for r in conn.execute(
            "SELECT * FROM 日界旁路凭证 WHERE 业务键=? ORDER BY 凭证号", (键,)).fetchall()]
        悬 = [dict(r) for r in conn.execute(
            "SELECT * FROM 悬账单 WHERE 业务键=? ORDER BY 编号", (键,)).fetchall()]
        跨日差 = [r for r in 差行 if r["跨日"] == 1]
        valid = all(_差行有效(conn, r) for r in
                    conn.execute("SELECT * FROM 对齐差 WHERE 业务键=?", (键,)).fetchall())
        valid = valid and all(_凭证有效(conn, r) for r in
                              conn.execute("SELECT * FROM 日界旁路凭证 WHERE 业务键=?", (键,)).fetchall())
        if 凭证 and not 跨日差:
            形态 = "禁跨:无对齐差,读旁路凭证集"
        elif 跨日差:
            形态 = "打标:有跨日对齐差行,读差行引用"
        elif 差行:
            形态 = "同日对齐差"
        else:
            形态 = "无日界记录"
        return {
            "业务键": 键,
            "策略": 当前策略(),
            "形态": 形态,
            "有跨日对齐差行": bool(跨日差),
            "有旁路凭证": bool(凭证),
            "对齐差行": 差行,
            "旁路凭证": 凭证,
            "悬账单": 悬,
            "valid": valid,
        }
    finally:
        conn.close()
