"""跨复盘并入（merge）HTTP API 端到端测试：python tests/test_api_merge.py

覆盖：
- POST .../merge/preview 只读预览：复制事件、两端入选关系、合并推算结论；
- 预览不推进目标版本；确认一次性保存为目标新草稿版本（kind=merge，记录来源）；
- 复制事件保留时区/偏移/持续/备注，关系保留等待上下界；复制事件在目标取
  新 uid、目标旧事件 uid 不变；来源历史不变；
- 冲突/暂存合并结果可保存（新版本草稿）但 finalize 仍 400；
- 拒绝路径：来源与目标相同(400)、事件不存在(400)、空选择(400)、参数缺失(400)、
  来源版本不存在(404)、来源缺快照(422)、base_version 过期(409)、签名不一致(409)，
  且失败后目标无部分写入。
"""
import json
import os
import pathlib
import sqlite3
import sys
import tempfile
import threading
import urllib.error
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["DB_PATH"] = _tmp.name
os.environ["PORT"] = "0"

import db  # noqa: E402
import server  # noqa: E402

db.init_db(server.DB_PATH)
HTTPD = server.ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
PORT = HTTPD.server_address[1]
threading.Thread(target=HTTPD.serve_forever, daemon=True).start()


def req(method, path, body=None):
    url = "http://127.0.0.1:{}{}".format(PORT, path)
    data = None if body is None else json.dumps(body).encode("utf-8")
    r = urllib.request.Request(url, data=data, method=method,
                               headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(r) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def ev(key, name, es, ls, dur, tz=None, note=""):
    e = {"key": key, "name": name, "earliest_start": es, "latest_start": ls,
         "duration_minutes": dur, "note": note}
    if tz is not None:
        e["timezone"] = tz
    return e


def make_source():
    status, b = req("POST", "/api/reviews", {"title": "来源复盘"})
    assert status == 201, b
    sid = b["review"]["id"]
    status, b = req("PUT", "/api/reviews/{}".format(sid), {
        "base_version": 1, "title": "来源复盘",
        "events": [
            ev("a", "上海告警", "2026-10-01T10:00+08:00", "2026-10-01T10:00+08:00",
               15, "Asia/Shanghai", note="来源备注"),
            ev("b", "纽约响应", "2026-09-30T20:00-04:00", "2026-10-01T12:00-04:00",
               30, "America/New_York"),
            ev("c", "待定", None, None, None, "Asia/Shanghai"),
        ],
        "relations": [
            {"predecessor_key": "a", "successor_key": "b",
             "min_gap_minutes": 10, "max_gap_minutes": 60},
            {"predecessor_key": "a", "successor_key": "c"},
        ]})
    assert status == 200, b
    return sid


def make_target():
    status, b = req("POST", "/api/reviews", {"title": "目标复盘"})
    assert status == 201, b
    tid = b["review"]["id"]
    status, b = req("PUT", "/api/reviews/{}".format(tid), {
        "base_version": 1, "title": "目标复盘",
        "events": [ev("x", "本地根因会", "2026-11-05T14:00", "2026-11-05T15:00", 60)],
        "relations": []})
    assert status == 200, b
    return tid


def snapshot_keys(rid, version):
    status, v = req("GET", "/api/reviews/{}/versions/{}".format(rid, version))
    assert status == 200, v
    return [e["key"] for e in v["input"]["events"]]


def test_preview_is_read_only_and_shows_plan():
    sid, tid = make_source(), make_target()
    keys = snapshot_keys(sid, 2)
    status, pv = req("POST", "/api/reviews/{}/merge/preview".format(tid),
                     {"source_review_id": sid, "source_version": 2,
                      "event_keys": keys[:2]})
    assert status == 200, pv
    assert pv["target_version"] == 2 and pv["source_version"] == 2
    assert pv["counts"] == {"source_events_total": 3, "target_events": 1,
                            "copied_events": 2, "target_relations": 0,
                            "copied_relations": 1, "skipped_relations": 1}
    # 合并推算：目标 0 号、复制 1/2 号；B 的窗口按纽约时间展示
    win_b = pv["result"]["windows"]["2"]
    assert win_b["earliest_start"] == "2026-09-30T22:25-04:00"
    assert win_b["latest_start"] == "2026-09-30T23:15-04:00"
    assert win_b["timezone"] == "America/New_York"
    assert pv["events"]["copied"][0]["note"] == "来源备注"
    assert "uid" not in pv["events"]["copied"][0]
    assert "uid" in pv["events"]["target"][0]
    assert pv["signature"]
    # 未复制关系带原因
    assert any("未入选" in s["reason"] for s in pv["relations"]["skipped"])
    # 预览不推进版本、不改输入
    status, cur = req("GET", "/api/reviews/{}".format(tid))
    assert cur["review"]["version"] == 2 and len(cur["events"]) == 1
    # 不传 event_keys = 全选
    status, pvall = req("POST", "/api/reviews/{}/merge/preview".format(tid),
                        {"source_review_id": sid, "source_version": 2})
    assert status == 200 and pvall["counts"]["copied_events"] == 3
    assert pvall["counts"]["copied_relations"] == 2


def test_confirm_saves_merge_version_with_new_identities():
    sid, tid = make_source(), make_target()
    _, tcur = req("GET", "/api/reviews/{}".format(tid))
    tgt_uid = tcur["events"][0]["uid"]
    src_uids = {e["name"]: e["uid"]
                for e in req("GET", "/api/reviews/{}".format(sid))[1]["events"]}
    keys = snapshot_keys(sid, 2)[:2]
    _, pv = req("POST", "/api/reviews/{}/merge/preview".format(tid),
                {"source_review_id": sid, "source_version": 2, "event_keys": keys})
    status, b = req("POST", "/api/reviews/{}/merge/confirm".format(tid),
                    {"source_review_id": sid, "source_version": 2, "event_keys": keys,
                     "base_version": 2, "signature": pv["signature"]})
    assert status == 200, b
    assert b["review"]["version"] == 3 and b["review"]["status"] == "draft"
    assert len(b["events"]) == 3 and len(b["relations"]) == 1
    # 目标身份保留、复制身份全新
    assert next(e for e in b["events"] if e["name"] == "本地根因会")["uid"] == tgt_uid
    for name in ("上海告警", "纽约响应"):
        u = next(e["uid"] for e in b["events"] if e["name"] == name)
        assert u and u not in src_uids.values()
    # 字段保留
    a = next(e for e in b["events"] if e["name"] == "上海告警")
    assert a["timezone"] == "Asia/Shanghai"
    assert a["earliest_start"] == "2026-10-01T10:00+08:00" and a["note"] == "来源备注"
    r = b["relations"][0]
    assert r["min_gap_minutes"] == 10 and r["max_gap_minutes"] == 60
    # 新快照为 merge 并记录来源
    status, mv = req("GET", "/api/reviews/{}/versions/3".format(tid))
    assert status == 200
    assert mv["kind"] == "merge" and mv["source_review_id"] == sid
    assert mv["source_version"] == 2
    # 来源历史与当前版本不变
    _, sv2 = req("GET", "/api/reviews/{}/versions/2".format(sid))
    assert len(sv2["input"]["events"]) == 3
    assert req("GET", "/api/reviews/{}".format(sid))[1]["review"]["version"] == 2
    # 版本列表标记并入产生
    _, vl = req("GET", "/api/reviews/{}/versions".format(tid))
    v3 = next(v for v in vl["versions"] if v["version"] == 3)
    assert v3["kind"] == "merge" and v3["reviewable"] and v3["restorable"]


def test_conflicting_merge_saved_but_finalize_rejected():
    sid = req("POST", "/api/reviews", {"title": "冲突源"})[1]["review"]["id"]
    status, _ = req("PUT", "/api/reviews/{}".format(sid), {
        "base_version": 1, "title": "冲突源",
        "events": [ev("p", "P", "2026-10-02T10:00", "2026-10-02T10:00", 0),
                   ev("q", "Q", "2026-10-02T09:00", "2026-10-02T09:00", 0)],
        "relations": [{"predecessor_key": "p", "successor_key": "q"}]})
    assert status == 200
    tid = req("POST", "/api/reviews", {"title": "T"})[1]["review"]["id"]
    _, pv = req("POST", "/api/reviews/{}/merge/preview".format(tid),
                {"source_review_id": sid, "source_version": 2})
    assert pv["result"]["status"] == "conflict"
    status, b = req("POST", "/api/reviews/{}/merge/confirm".format(tid),
                    {"source_review_id": sid, "source_version": 2,
                     "base_version": 1, "signature": pv["signature"]})
    assert status == 200 and b["review"]["status"] == "draft"
    status, resp = req("POST", "/api/reviews/{}/finalize".format(tid),
                       {"base_version": 2})
    assert status == 400, resp


def test_rejections_and_no_partial_writes():
    sid, tid = make_source(), make_target()
    keys = snapshot_keys(sid, 2)
    _, pv = req("POST", "/api/reviews/{}/merge/preview".format(tid),
                {"source_review_id": sid, "source_version": 2, "event_keys": keys[:2]})

    def preview(body):
        return req("POST", "/api/reviews/{}/merge/preview".format(tid), body)

    status, e = preview({"source_review_id": tid, "source_version": 2})
    assert status == 400 and "必须不同" in e["error"], e
    status, e = preview({"source_review_id": sid, "source_version": 2,
                         "event_keys": ["不存在"]})
    assert status == 400 and "不存在" in e["error"], e
    status, e = preview({"source_review_id": sid, "source_version": 2,
                         "event_keys": []})
    assert status == 400 and "至少" in e["error"], e
    status, e = preview({"source_version": 2})
    assert status == 400
    status, e = preview({"source_review_id": sid, "source_version": 99})
    assert status == 404, e
    status, e = preview({"source_review_id": 9999, "source_version": 1})
    assert status == 404, e

    # 来源版本缺快照 -> 422（删来源 v1 快照）
    conn = sqlite3.connect(server.DB_PATH)
    conn.execute("DELETE FROM snapshots WHERE review_id=? AND review_version=1", (sid,))
    conn.commit()
    conn.close()
    status, e = preview({"source_review_id": sid, "source_version": 1})
    assert status == 422 and "快照" in e["error"], e

    # 确认缺签名 -> 400
    status, e = req("POST", "/api/reviews/{}/merge/confirm".format(tid),
                    {"source_review_id": sid, "source_version": 2, "base_version": 2})
    assert status == 400 and "signature" in e["error"]
    # base_version 过期 -> 409（用有效签名）
    status, _ = req("PUT", "/api/reviews/{}".format(tid), {
        "base_version": 2, "title": "目标复盘",
        "events": [ev("x", "本地根因会", "2026-11-05T14:00", "2026-11-05T15:00", 60)],
        "relations": []})
    assert status == 200
    status, e = req("POST", "/api/reviews/{}/merge/confirm".format(tid),
                    {"source_review_id": sid, "source_version": 2, "event_keys": keys[:2],
                     "base_version": 2, "signature": pv["signature"]})
    assert status == 409 and e["current_version"] == 3, e
    # 目标仍只有 1 个事件（无部分写入）
    _, cur = req("GET", "/api/reviews/{}".format(tid))
    assert len(cur["events"]) == 1

    # 目标未变但选择集/签名不一致 -> 409
    status, e = req("POST", "/api/reviews/{}/merge/confirm".format(tid),
                    {"source_review_id": sid, "source_version": 2,
                     "event_keys": keys[:1], "base_version": 3,
                     "signature": pv["signature"]})
    assert status == 409 and e["current_version"] == 3, e
    assert req("GET", "/api/reviews/{}".format(tid))[1]["review"]["version"] == 3


def test_merge_preview_result_equals_confirmed_result():
    sid, tid = make_source(), make_target()
    keys = snapshot_keys(sid, 2)[:2]
    _, pv = req("POST", "/api/reviews/{}/merge/preview".format(tid),
                {"source_review_id": sid, "source_version": 2, "event_keys": keys})
    _, b = req("POST", "/api/reviews/{}/merge/confirm".format(tid),
               {"source_review_id": sid, "source_version": 2, "event_keys": keys,
                "base_version": 2, "signature": pv["signature"]})
    stored = b["computation"]["result"]
    row_ids = [e["id"] for e in b["events"]]  # 行顺序即合并序号
    for ordinal, rid in enumerate(row_ids):
        pw, sw = pv["result"]["windows"][str(ordinal)], stored["windows"][str(rid)]
        for f in ("timezone", "earliest_start", "latest_start",
                  "earliest_end", "latest_end"):
            assert pw[f] == sw[f], (ordinal, f)
    assert stored["status"] == pv["result"]["status"]


TESTS = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    failed = 0
    for t in TESTS:
        try:
            t()
            print("PASS", t.__name__)
        except AssertionError as e:
            failed += 1
            print("FAIL", t.__name__, "->", e)
    HTTPD.shutdown()
    sys.exit(1 if failed else 0)
