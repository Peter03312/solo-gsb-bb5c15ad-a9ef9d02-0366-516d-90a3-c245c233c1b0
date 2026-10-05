"""跨复盘并入（merge）单元测试：python tests/test_merge.py

覆盖：
- 预览只读：不改目标版本/输入，来源历史不变；
- 复制事件保留时间范围、持续分钟、备注、IANA 时区与 UTC 偏移，
  关系保留等待上下界，仅复制两端均入选的关系；
- 复制来的事件在目标取得新 uid（新持续身份），目标旧事件 uid 不变；
- 确认成功后一次性保存为目标新草稿版本（kind='merge'，记录来源复盘/版本），
  定稿目标并入后回到草稿；冲突/暂存合并结果可保存但不得定稿；
- 推算结论与预览一致（按序号重编号，与临时 key 无关）；
- 拒绝路径：来源与目标相同、事件不存在、选择为空/重复、来源版本不存在、
  来源版本缺快照（旧库 422 语义），均无部分写入；
- base_version 过期抛 VersionConflict（409）；签名不一致抛 StalePreview（409）。
"""
import pathlib
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "app"))

import db  # noqa: E402
import merge as merge_mod  # noqa: E402


def ev(key, name, es, ls, dur, tz=None, note=""):
    e = {"key": key, "name": name, "earliest_start": es, "latest_start": ls,
         "duration_minutes": dur, "note": note}
    if tz is not None:
        e["timezone"] = tz
    return e


def rel(a, b, min_g=None, max_g=None):
    r = {"predecessor_key": a, "successor_key": b}
    if min_g is not None:
        r["min_gap_minutes"] = min_g
    if max_g is not None:
        r["max_gap_minutes"] = max_g
    return r


def setup_db():
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    db.init_db(tmp.name)
    return tmp.name


def build_source(p, title="来源复盘", version_inputs=None):
    """构造一个带历史版本的来源复盘，返回 (path, review_id)。

    默认 v2：A(上海,tz) 先于 B(纽约,tz) 且最短等待 10；另有暂存事件 C 与
    A→C 关系（用于验证只复制两端入选的关系）。
    """
    sid = db.create_review(p, title)
    events = [
        ev("a", "上海告警", "2026-10-01T10:00+08:00", "2026-10-01T10:00+08:00",
           15, "Asia/Shanghai", note="来源备注"),
        ev("b", "纽约响应", "2026-09-30T20:00-04:00", "2026-10-01T12:00-04:00",
           30, "America/New_York"),
        ev("c", "待定电话会", None, None, None),
    ]
    db.update_review(p, sid, 1, title, events, [
        rel("a", "b", min_g=10, max_g=60),
        rel("a", "c"),
    ])
    return sid


def build_target(p, title="目标复盘"):
    tid = db.create_review(p, title)
    db.update_review(p, tid, 1, title, [
        ev("x", "本地根因会", "2026-11-05T14:00", "2026-11-05T15:00", 60),
    ], [])
    return tid


def source_keys(p, sid, version=2):
    data = db.get_version(p, sid, version)
    return [e["key"] for e in data["input"]["events"]]


# ---------------------------------------------------------------------- 纯计划

def test_plan_selects_only_both_end_relations():
    p = setup_db()
    sid = build_source(p)
    tid = build_target(p)
    keys = source_keys(p, sid)
    a_key, b_key, c_key = keys  # 第三个是暂存事件 C

    pv = db.preview_merge(p, tid, sid, 2, [a_key, b_key])
    assert pv["counts"]["copied_events"] == 2
    assert pv["counts"]["copied_relations"] == 1   # A→B 两端入选
    assert pv["counts"]["skipped_relations"] == 1  # A→C 因 C 未选跳过
    cp = pv["relations"]["copied"][0]
    assert cp["predecessor"]["name"] == "上海告警"
    assert cp["successor"]["name"] == "纽约响应"
    assert cp["min_gap_minutes"] == 10 and cp["max_gap_minutes"] == 60
    assert "未入选" in pv["relations"]["skipped"][0]["reason"]

    # 全选：暂存事件也复制，A→C 关系随之复制（但推算时因 C 暂存被跳过）
    pv_all = db.preview_merge(p, tid, sid, 2, None)
    assert pv_all["counts"]["copied_events"] == 3
    assert pv_all["counts"]["copied_relations"] == 2
    assert {d["name"] for d in pv_all["result"]["drafts"]} == {"待定电话会"}


def test_plan_preserves_fields_and_recomputes_by_ordinal():
    p = setup_db()
    sid = build_source(p)
    tid = build_target(p)
    keys = source_keys(p, sid)
    pv = db.preview_merge(p, tid, sid, 2, keys[:2])

    # 目标在前(序号 0)，复制事件在后(序号 1/2)
    assert [e["compute_id"] for e in pv["events"]["target"]] == [0]
    assert [e["compute_id"] for e in pv["events"]["copied"]] == [1, 2]
    copied_a = pv["events"]["copied"][0]
    assert copied_a["timezone"] == "Asia/Shanghai"
    assert copied_a["earliest_start"] == "2026-10-01T10:00+08:00"
    assert copied_a["note"] == "来源备注"
    assert copied_a["duration_minutes"] == 15
    assert "uid" not in copied_a  # 复制事件不带来源身份

    res = pv["result"]
    assert res["status"] == "ok"
    # 上海 10:00+08 + 15 + 最短等待 10 = 02:25 UTC = 纽约 09-30 22:25-04；
    # 最晚不晚于 上海最晚结束 10:15+08 + 最长等待 60 = 03:15 UTC = 23:15-04
    win_b = res["windows"][2]
    assert win_b["earliest_start"] == "2026-09-30T22:25-04:00"
    assert win_b["latest_start"] == "2026-09-30T23:15-04:00"
    assert win_b["timezone"] == "America/New_York"


def test_signature_stable_across_independent_plans():
    # 预览/确认是两次独立的计划构建（临时 key 不同），但按序号重算的
    # 推算结论与签名必须完全一致。
    p = setup_db()
    sid = build_source(p)
    tid = build_target(p)
    keys = source_keys(p, sid)[:2]
    pv1 = db.preview_merge(p, tid, sid, 2, keys)
    pv2 = db.preview_merge(p, tid, sid, 2, keys)
    assert pv1["signature"] == pv2["signature"]
    assert pv1["result"] == pv2["result"]


# ---------------------------------------------------------------------- 预览只读

def test_preview_is_read_only():
    p = setup_db()
    sid = build_source(p)
    tid = build_target(p)
    before = db.get_bundle(p, tid)
    pv = db.preview_merge(p, tid, sid, 2, source_keys(p, sid))
    after = db.preview_merge(p, tid, sid, 2, source_keys(p, sid))
    cur = db.get_bundle(p, tid)
    assert cur["review"]["version"] == before["review"]["version"] == 2
    assert len(cur["events"]) == 1
    assert after["target_version"] == 2
    # 来源当前版本也不变
    assert db.get_bundle(p, sid)["review"]["version"] == 2
    assert pv["signature"]


# ---------------------------------------------------------------------- 确认写入

def test_confirm_saves_new_draft_with_new_identities():
    p = setup_db()
    sid = build_source(p)
    tid = build_target(p)

    src_uids = {e["name"]: e["uid"] for e in db.get_bundle(p, sid)["events"]}
    tgt_uid_before = db.get_bundle(p, tid)["events"][0]["uid"]

    keys = source_keys(p, sid)[:2]
    pv = db.preview_merge(p, tid, sid, 2, keys)
    new_v = db.merge_review(p, tid, sid, 2, keys, 2, pv["signature"])
    assert new_v == 3

    b = db.get_bundle(p, tid)
    assert b["review"]["version"] == 3
    assert b["review"]["status"] == "draft"
    assert len(b["events"]) == 3 and len(b["relations"]) == 1

    # 目标事件身份保留
    assert next(e for e in b["events"] if e["name"] == "本地根因会")["uid"] \
        == tgt_uid_before
    # 复制事件取得全新身份，且彼此不同、不同于来源
    new_uids = [next(e["uid"] for e in b["events"] if e["name"] == n)
                for n in ("上海告警", "纽约响应")]
    assert new_uids[0] != new_uids[1]
    assert all(u and u not in src_uids.values() for u in new_uids)

    # 关系等待上下界保留，且只连复制事件
    r = b["relations"][0]
    assert r["min_gap_minutes"] == 10 and r["max_gap_minutes"] == 60
    ids = {e["id"] for e in b["events"]}
    assert r["predecessor_id"] in ids and r["successor_id"] in ids

    # 快照 kind=merge 且记录来源
    mv = db.get_version(p, tid, 3)
    assert mv["kind"] == "merge" and mv["state"] == "draft"
    assert mv["source_review_id"] == sid and mv["source_version"] == 2
    assert [e["name"] for e in mv["input"]["events"]] == \
        ["本地根因会", "上海告警", "纽约响应"]

    # 来源历史未变
    sv2 = db.get_version(p, sid, 2)
    assert len(sv2["input"]["events"]) == 3
    assert db.get_bundle(p, sid)["review"]["version"] == 2
    # 目标旧版本快照未变
    assert [e["name"] for e in db.get_version(p, tid, 2)["input"]["events"]] == \
        ["本地根因会"]


def test_confirm_result_equals_preview():
    p = setup_db()
    sid = build_source(p)
    tid = build_target(p)
    keys = source_keys(p, sid)[:2]
    pv = db.preview_merge(p, tid, sid, 2, keys)
    db.merge_review(p, tid, sid, 2, keys, 2, pv["signature"])
    b = db.get_bundle(p, tid)
    stored = b["computation"]["result"]
    # live 行顺序即合并顺序：序号 0/1/2 -> 行 id 依次
    row_ids = [e["id"] for e in b["events"]]
    for ordinal, rid in enumerate(row_ids):
        pw, sw = pv["result"]["windows"][ordinal], stored["windows"][str(rid)]
        for f in ("timezone", "earliest_start", "latest_start",
                  "earliest_end", "latest_end"):
            assert pw[f] == sw[f], (ordinal, f, pw[f], sw[f])
        assert pw["earliest_chain"] == sw["earliest_chain"]
        assert pw["latest_chain"] == sw["latest_chain"]
    assert stored["status"] == pv["result"]["status"]


def test_merge_finalized_target_returns_to_draft():
    p = setup_db()
    sid = build_source(p)
    tid = build_target(p)
    db.finalize_review(p, tid, 2)  # v3 定稿
    assert db.get_bundle(p, tid)["review"]["status"] == "finalized"
    pv = db.preview_merge(p, tid, sid, 2, source_keys(p, sid)[:2])
    db.merge_review(p, tid, sid, 2, source_keys(p, sid)[:2], 3, pv["signature"])
    b = db.get_bundle(p, tid)
    assert b["review"]["version"] == 4 and b["review"]["status"] == "draft"


def test_conflicting_merge_saved_but_not_finalizable():
    p = setup_db()
    csid = db.create_review(p, "冲突来源")
    db.update_review(p, csid, 1, "冲突来源", [
        ev("p", "P", "2026-10-02T10:00", "2026-10-02T10:00", 0),
        ev("q", "Q", "2026-10-02T09:00", "2026-10-02T09:00", 0),
    ], [rel("p", "q")])  # v2：P 先于 Q 但固定时刻矛盾
    tid = db.create_review(p, "T")
    pv = db.preview_merge(p, tid, csid, 2, None)
    assert pv["result"]["status"] == "conflict"
    db.merge_review(p, tid, csid, 2, None, 1, pv["signature"])
    b = db.get_bundle(p, tid)
    assert b["review"]["status"] == "draft"
    assert b["computation"]["status"] == "conflict"
    try:
        db.finalize_review(p, tid, 2)
        assert False, "冲突的并入版本不得定稿"
    except db.FinalizeRejected:
        pass


# ---------------------------------------------------------------------- 拒绝路径

def test_same_review_rejected():
    p = setup_db()
    tid = build_target(p)
    for fn in (lambda: db.preview_merge(p, tid, tid, 2, None),
               lambda: db.merge_review(p, tid, tid, 2, None, 2, "x")):
        try:
            fn()
            assert False
        except db.InvalidMerge:
            pass
    assert db.get_bundle(p, tid)["review"]["version"] == 2


def test_nonexistent_event_keys_rejected_without_write():
    p = setup_db()
    sid = build_source(p)
    tid = build_target(p)
    for keys in (["不存在的key"], [], ["x", "x"]):
        try:
            db.preview_merge(p, tid, sid, 2, keys)
            assert False, keys
        except db.InvalidMerge:
            pass
    # None 类型/含空串
    try:
        db.preview_merge(p, tid, sid, 2, [""])
        assert False
    except db.InvalidMerge:
        pass
    assert db.get_bundle(p, tid)["review"]["version"] == 2


def test_missing_version_and_review():
    p = setup_db()
    sid = build_source(p)
    tid = build_target(p)
    try:
        db.preview_merge(p, tid, sid, 99, None)
        assert False
    except db.VersionNotFound:
        pass
    try:
        db.preview_merge(p, tid, 9999, 1, None)
        assert False
    except db.NotFound:
        pass
    try:
        db.preview_merge(p, 9999, sid, 1, None)
        assert False
    except db.NotFound:
        pass


def test_legacy_source_snapshot_rejected():
    p = setup_db()
    sid = build_source(p)
    tid = build_target(p)
    # 删掉来源 v1 的快照，模拟旧库历史版本
    conn = sqlite3.connect(p)
    conn.execute("DELETE FROM snapshots WHERE review_id=? AND review_version=1", (sid,))
    conn.commit()
    conn.close()
    try:
        db.preview_merge(p, tid, sid, 1, None)
        assert False
    except db.LegacyVersion:
        pass
    pv = db.preview_merge(p, tid, sid, 2, None)
    try:
        db.merge_review(p, tid, sid, 1, None, 2, pv["signature"])
        assert False
    except db.LegacyVersion:
        pass
    assert db.get_bundle(p, tid)["review"]["version"] == 2


def test_stale_base_version_conflicts_without_write():
    p = setup_db()
    sid = build_source(p)
    tid = build_target(p)
    pv = db.preview_merge(p, tid, sid, 2, None)
    # 目标被他人保存推进到 v3
    db.update_review(p, tid, 2, "目标复盘-改",
                     [ev("z", "新事件", "2026-11-05T14:00", "2026-11-05T15:00", 60)], [])
    try:
        db.merge_review(p, tid, sid, 2, None, 2, pv["signature"])
        assert False
    except db.VersionConflict as e:
        assert e.current_version == 3
    b = db.get_bundle(p, tid)
    assert len(b["events"]) == 1 and b["events"][0]["name"] == "新事件"


def test_signature_mismatch_conflicts_without_write():
    p = setup_db()
    sid = build_source(p)
    tid = build_target(p)
    pv = db.preview_merge(p, tid, sid, 2, None)
    # 目标版本未变，但确认的选择集与预览不一致（只复制前一个事件）
    one_key = source_keys(p, sid)[:1]
    try:
        db.merge_review(p, tid, sid, 2, one_key, 2, pv["signature"])
        assert False
    except db.StalePreview:
        pass
    assert db.get_bundle(p, tid)["review"]["version"] == 2
    assert len(db.get_bundle(p, tid)["events"]) == 1


def test_no_cross_group_timezone_relations():
    # 复制组内部若同为带时区事件则关系正常；合并不会产生跨组关系，
    # 因此带时区复制事件与朴素目标事件之间不存在关系连接。
    p = setup_db()
    sid = build_source(p)
    tid = build_target(p)
    keys = source_keys(p, sid)[:2]
    pv = db.preview_merge(p, tid, sid, 2, keys)
    assert pv["result"]["status"] == "ok"
    db.merge_review(p, tid, sid, 2, keys, 2, pv["signature"])
    b = db.get_bundle(p, tid)
    x = next(e for e in b["events"] if e["name"] == "本地根因会")
    involved = set()
    for r in b["relations"]:
        involved.add(r["predecessor_id"])
        involved.add(r["successor_id"])
    assert x["id"] not in involved


def test_merge_plan_module_validates_selection():
    target = {"id": 1, "title": "t", "version": 1,
              "input": {"events": [], "relations": []}}
    source = {"id": 2, "title": "s", "version": 1,
              "input": {"events": [], "relations": []}}
    try:
        merge_mod.build_merge_plan(target, source, None)
        assert False, "空来源必须拒绝"
    except merge_mod.InvalidMergeSelection:
        pass


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
    sys.exit(1 if failed else 0)
