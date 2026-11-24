import os
import time
from pathlib import Path

_tmp = Path(__file__).resolve().parent / "_tmp"
_tmp.mkdir(exist_ok=True)
os.environ["数据目录"] = str(_tmp)
os.environ["夹具目录"] = str(Path(__file__).resolve().parents[2] / "fixtures")
os.environ["TZ"] = "UTC"
time.tzset()

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app import seed
from app.db import 连接
from app.services import 左装载, 右装载, 对齐, 结论, 目录
from app.modules import 日界切开禁跨 as 日界

client = TestClient(app)


def setup_function():
    db = _tmp / "对拍.db"
    if db.exists():
        db.unlink()
    seed.初始化库()


def _装跨日():
    # K1：左 11-23、右 11-24，跨业务日同键（金额相等也不得压成一笔）
    左装载.装一批({"事件": [
        {"事件编号": "L1", "业务键": "K1", "金额分": 100, "源键": "渠甲", "事件时": "2026-11-23T12:00:00Z"},
        {"事件编号": "L2", "业务键": "K2", "金额分": 200, "源键": "渠甲", "事件时": "2026-11-23T12:05:00Z"},
    ]})
    右装载.装一批({"事件": [
        {"事件编号": "R1", "业务键": "K1", "金额分": 100, "源键": "核乙", "事件时": "2026-11-24T01:00:00Z"},
        {"事件编号": "R2", "业务键": "K2", "金额分": 200, "源键": "核乙", "事件时": "2026-11-23T12:06:00Z"},
    ]})


def test_同日双边对齐带业务日():
    左装载.装一批({"事件": [
        {"事件编号": "L1", "业务键": "K2", "金额分": 200, "源键": "渠甲", "事件时": "2026-11-23T12:05:00Z"},
    ]})
    右装载.装一批({"事件": [
        {"事件编号": "R2", "业务键": "K2", "金额分": 200, "源键": "核乙", "事件时": "2026-11-23T12:06:00Z"},
    ]})
    r = 日界.跑日界对齐()
    assert r["同日对齐"] == 1 and r["跨日键"] == 0
    行 = [x for x in 目录.差列表() if x["业务键"] == "K2"][0]
    assert 行["差型"] == "对齐"
    assert 行["左业务日"] == "2026-11-23" and 行["右业务日"] == "2026-11-23"
    assert 行["跨日"] == 0
    # 锁必须释放
    conn = 连接()
    assert conn.execute("SELECT COUNT(*) AS c FROM 对齐锁").fetchone()["c"] == 0
    conn.close()


def test_禁跨形态_无对齐差有旁路凭证():
    _装跨日()
    assert 日界.当前策略() == "禁跨"
    r = 日界.跑日界对齐()
    assert r["跨日键"] == 1 and r["旁路凭证"] == 2

    # 跨日同键 K1：不存在任何对齐差行（切日写差禁跨同成败——禁跨即不写双边差）
    k1差 = [x for x in 目录.差列表() if x["业务键"] == "K1"]
    assert k1差 == []
    # 同日 K2 仍正常对齐
    k2差 = [x for x in 目录.差列表() if x["业务键"] == "K2"]
    assert len(k2差) == 1 and k2差[0]["差型"] == "对齐"

    # 两侧旁路凭证钉跨日
    视图 = 日界.日界列表()
    票 = [x for x in 视图 if x["业务键"] == "K1"]
    assert len(票) == 2 and all(x["差或旁路"] == "禁跨旁路" for x in 票)
    assert {x["左日"] for x in 票} == {"2026-11-23"}
    assert {x["右日"] for x in 票} == {"2026-11-24"}
    assert all(x["跨日"] == 1 and x["valid"] for x in 票)
    assert all(x["凭证号"].startswith("BP-") for x in 票)

    # 单键视图可区分两种形态
    单 = 日界.日界单键("K1")
    assert 单["有跨日对齐差行"] is False and 单["有旁路凭证"] is True
    assert 单["形态"].startswith("禁跨")

    # 悬账由旁路凭证背书，结论改读旁路集 → 通过
    g = 结论.跑一次()
    assert g["是否通过"] is True and g["旁路凭证数"] == 2


def test_打标形态_有跨日差无旁路():
    _装跨日()
    日界.改策略("打标")
    r = 日界.跑日界对齐()
    assert r["跨日键"] == 1 and r["旁路凭证"] == 0

    k1差 = [x for x in 目录.差列表() if x["业务键"] == "K1"]
    assert len(k1差) == 1
    assert k1差[0]["跨日"] == 1
    assert k1差[0]["左业务日"] == "2026-11-23" and k1差[0]["右业务日"] == "2026-11-24"

    视图 = [x for x in 日界.日界列表() if x["业务键"] == "K1"]
    assert len(视图) == 1 and 视图[0]["差或旁路"] == "对齐" and 视图[0]["凭证号"] == ""

    单 = 日界.日界单键("K1")
    assert 单["有跨日对齐差行"] is True and 单["有旁路凭证"] is False
    assert 单["形态"].startswith("打标")

    g = 结论.跑一次()
    assert g["是否通过"] is True and g["跨日差笔数"] == 1 and g["旁路凭证数"] == 0


def test_策略切换重算不残留():
    _装跨日()
    日界.跑日界对齐()
    assert len(日界.日界列表()) >= 2  # 禁跨：旁路凭证
    日界.改策略("打标")
    日界.跑日界对齐()
    视图 = 日界.日界列表()
    assert all(x["差或旁路"] != "禁跨旁路" for x in 视图)
    g = 结论.跑一次()
    assert g["是否通过"] is True


def test_切日与对齐并发不得双落():
    _装跨日()
    # 外部连接持锁不放，模拟编排进行中
    持锁 = 连接()
    持锁.execute("INSERT INTO 对齐锁(锁键,持有者,取得时) VALUES (?,?,?)",
                 (日界.锁键, "测试持锁", seed.此刻()))
    持锁.commit()
    try:
        # 基线对齐抢锁失败 → 409，不写任何结果
        with pytest.raises(Exception):
            对齐.跑对齐()
        # 日界编排自身也抢不到 → 日界冲突
        with pytest.raises(日界.日界冲突):
            日界.跑日界对齐()
        # 结果表保持空白：无双落
        conn = 连接()
        assert conn.execute("SELECT COUNT(*) AS c FROM 对齐差").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM 悬账单").fetchone()["c"] == 0
        assert conn.execute("SELECT COUNT(*) AS c FROM 日界旁路凭证").fetchone()["c"] == 0
        conn.close()

        # HTTP 层 409
        resp = client.post("/api/对齐/跑")
        assert resp.status_code == 409
        resp2 = client.post("/api/日界/跑")
        assert resp2.status_code == 409
    finally:
        持锁.execute("DELETE FROM 对齐锁")
        持锁.commit()
        持锁.close()

    # 锁释放后两边都能跑，但后跑整体覆盖，仍是单一结果集
    对齐.跑对齐()
    r = 日界.跑日界对齐()
    assert r["旁路凭证"] == 2
    conn = 连接()
    assert conn.execute("SELECT COUNT(*) AS c FROM 对齐锁").fetchone()["c"] == 0
    conn.close()


def test_valid_漂移检测():
    _装跨日()
    日界.跑日界对齐()
    assert all(x["valid"] for x in 日界.日界列表() if x["业务键"] == "K1")
    # 流水被改：凭证金额与现额对不上 → valid 翻 False
    conn = 连接()
    conn.execute("UPDATE 左轨流水 SET 金额分=999 WHERE 事件编号='L1'")
    conn.commit()
    conn.close()
    票 = [x for x in 日界.日界列表() if x["业务键"] == "K1"]
    assert any(not x["valid"] for x in 票)
    g = 结论.跑一次()
    assert g["是否通过"] is False
    assert any("valid=0" in p for p in g["问题"])


def test_同日多笔按业务日汇总后比对():
    左装载.装一批({"事件": [
        {"事件编号": "L1", "业务键": "K9", "金额分": 100, "源键": "渠甲", "事件时": "2026-11-23T10:00:00Z"},
        {"事件编号": "L2", "业务键": "K9", "金额分": 50, "源键": "渠甲", "事件时": "2026-11-23T11:00:00Z"},
    ]})
    右装载.装一批({"事件": [
        {"事件编号": "R1", "业务键": "K9", "金额分": 150, "源键": "核乙", "事件时": "2026-11-23T12:00:00Z"},
    ]})
    日界.跑日界对齐()
    行 = [x for x in 目录.差列表() if x["业务键"] == "K9"][0]
    assert 行["差型"] == "对齐" and 行["左金额分"] == 150 and 行["跨日"] == 0


def test_只读接口_http字段():
    _装跨日()
    日界.跑日界对齐()
    resp = client.get("/api/日界")
    assert resp.status_code == 200
    必备 = {"业务键", "左日", "右日", "差或旁路", "valid"}
    assert 必备 <= set(resp.json()[0].keys())
    resp2 = client.get("/api/日界", params={"键": "K1"})
    assert resp2.status_code == 200 and resp2.json()["业务键"] == "K1"
    s = client.get("/api/日界策略").json()
    assert s["策略"] == "禁跨"
    bad = client.post("/api/日界策略", json={"策略": "乱讲"})
    assert bad.status_code == 400


def test_真实并发只有一个赢家():
    import threading
    _装跨日()
    进门 = threading.Event()
    放行 = threading.Event()
    原放锁 = 日界.放锁

    def 慢放锁(conn, 持有者):
        进门.set()
        放行.wait(timeout=5)
        return 原放锁(conn, 持有者)

    结果 = []
    日界.放锁 = 慢放锁
    try:
        def 日界跑():
            try:
                日界.跑日界对齐()
                结果.append(("ok", "日界"))
            except Exception as e:  # noqa: BLE001
                结果.append(("fail", type(e).__name__))

        t = threading.Thread(target=日界跑)
        t.start()
        进门.wait(timeout=5)  # 日界编排已完成并持锁停在放锁前
        # 锁窗口仍开着，基线对齐此刻抢锁必须失败
        try:
            对齐.跑对齐()
            结果.append(("ok", "基线"))
        except Exception as e:  # noqa: BLE001
            结果.append(("fail", type(e).__name__))
        放行.set()
        t.join()
    finally:
        日界.放锁 = 原放锁

    assert sorted(状态 for 状态, _ in 结果) == ["fail", "ok"]
    conn = 连接()
    # 结果只属于赢得锁的那次编排，锁已释放、无双落
    assert conn.execute("SELECT COUNT(*) AS c FROM 对齐锁").fetchone()["c"] == 0
    差数 = conn.execute("SELECT COUNT(*) AS c FROM 对齐差").fetchone()["c"]
    票数 = conn.execute("SELECT COUNT(*) AS c FROM 日界旁路凭证").fetchone()["c"]
    assert 差数 == 1 and 票数 == 2  # 日界编排赢：K2 同日差行 + K1 两张旁路，无 K1 双边差
    conn.close()


def test_基线主链不回归():
    左装载.装一批({"事件": [
        {"事件编号": "a1", "业务键": "K1", "金额分": 100, "源键": "渠甲", "事件时": "2026-11-23T10:00:00Z"},
        {"事件编号": "a2", "业务键": "K2", "金额分": 200, "源键": "渠甲", "事件时": "2026-11-23T10:00:01Z"},
    ]})
    右装载.装一批({"事件": [
        {"事件编号": "b1", "业务键": "K1", "金额分": 100, "源键": "核乙", "事件时": "2026-11-23T10:00:00Z"},
        {"事件编号": "b2", "业务键": "K3", "金额分": 50, "源键": "核乙", "事件时": "2026-11-23T10:00:02Z"},
    ]})
    r = 对齐.跑对齐()
    assert r["对齐"] == 1 and r["悬账"] >= 2
    assert len(目录.悬列表()) >= 2
    g = 结论.跑一次()
    assert g["是否通过"] is True
