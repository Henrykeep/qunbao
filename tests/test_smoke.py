"""冒烟测试：python -m pytest tests -q（需要 pip install fastapi httpx pytest）。不连真实大模型和 QQ。"""
import importlib, os, sys, tempfile, base64

os.environ.update(DB_PATH=os.path.join(tempfile.mkdtemp(), "t.db"), WEB_PASS="pw", INGEST_TOKEN="tok", LLM_API_KEY="")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "digest"))
app_mod = importlib.import_module("app")
from fastapi.testclient import TestClient

c = TestClient(app_mod.app)
AUTH = {"Authorization": "Basic " + base64.b64encode(b"me:pw").decode()}


def test_auth_required():
    assert c.get("/api/state").status_code == 401
    assert c.get("/api/state", headers=AUTH).status_code == 200


def test_onebot_group():
    e = {"post_type": "message", "message_type": "group", "group_id": 1, "self_id": 9, "user_id": 2,
         "sender": {"card": "王老师"}, "raw_message": "[CQ:at,qq=9] 周五前交报告", "time": 1}
    app_mod._group_names[1] = "计科2201"
    assert c.post("/onebot", json=e).status_code == 200
    ms = c.get("/api/messages?chat=计科2201", headers=AUTH).json()
    assert ms[-1]["text"] == "@我 周五前交报告" and ms[-1]["at_me"]


def test_wechat_formats():
    assert c.post("/ingest", json={"title": "x", "text": "y"}).status_code == 401
    r = c.post("/ingest?token=tok", json={"title": "班级群", "text": "[3条]李华: 明天 8 点集合"}).json()
    assert r["chat"] == "班级群" and r["sender"] == "李华"
    assert c.post("/ingest?token=tok", json={"title": "班级群", "text": "[3条]李华: 明天 8 点集合"}).json().get("dup")
    r = c.post("/ingest", headers={"X-Token": "tok"}, json={"title": "妈妈", "content": "吃饭了吗"}).json()
    assert r["chat"] == "私聊·妈妈"
    r = c.post("/ingest?token=tok", content="title=社团&content=%5B有人%40我%5D张三：@小明 来一下",
               headers={"content-type": "application/x-www-form-urlencoded"}).json()
    assert r["chat"] == "社团" and r["sender"] == "张三"
    assert c.post("/ingest?token=tok", json={"title": "微信", "text": "你收到了一条消息"}).json().get("skipped")
    ms = c.get("/api/messages?chat=社团&source=微信", headers=AUTH).json()
    assert ms[-1]["at_me"] and ms[-1]["text"] == "@小明 来一下"
    st = c.get("/api/state", headers=AUTH).json()["status"]
    assert st["wx"]["online"] and st["wx"]["ready"]
    chats = c.get("/api/chats?source=微信", headers=AUTH).json()
    assert {x["chat"] for x in chats} == {"班级群", "私聊·妈妈", "社团"}
