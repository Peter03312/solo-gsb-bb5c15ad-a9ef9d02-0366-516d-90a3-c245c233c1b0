"""把另一份复盘某历史版本的事件链并入当前复盘（纯函数，只读）。

合并规则（与持久化、HTTP 层无关，便于预览/确认复用同一份计划）：

- 目标复盘当前的全部事件与“先于”关系原样保留（持续身份 uid 不变）；
- 来源版本快照中被选中的事件复制到目标，复制时**不沿用来源 uid**——
  落库时由持久化层分配全新 uid，即“在目标取得新的持续身份”；事件的
  时间范围（含 UTC 偏移）、持续分钟、备注、IANA 时区原样保留；
- “先于”关系仅复制来源快照中**两端事件均入选**的那些，最短/最长等待
  上下界原样保留；任一端未入选的关系不复制并给出原因；
- 不产生任何跨组关系：目标旧关系只连目标事件，复制来的关系只连复制来
  的事件，因此带时区与未带时区事件不会被跨组连接。

为保证“预览所见即确认所得”，合并后的推算输入按**序号**重新编号
（目标事件按当前顺序在前、复制事件按来源快照顺序在后；关系同理：
目标关系在前、复制关系在后），推算结果中的窗口/冲突/暂存 id 均为该
序号；对推算结果做规范化序列化得到 signature，确认时重算并比对，
目标版本未变化时必须与预览完全一致。
"""

import itertools
import json

from compute import compute

# 模块内自增计数器：复制事件在合并计划中的临时 key 只在计划内部使用
# （关系引用需要），落库时会以行号重新生成快照 key，因此临时 key 无需
# 跨进程稳定，只需在一次计划内唯一。
_key_counter = itertools.count(1)


def default_new_key():
    return "merge_{}".format(next(_key_counter))


class InvalidMergeSelection(ValueError):
    """合并输入不合法（来源与目标相同、事件不存在、选择为空、key 重复等）。"""


def _gap_pair(r):
    return (r.get("min_gap_minutes") or 0), r.get("max_gap_minutes")


def _event_fields(e):
    """复制/展示事件时保留的输入字段（刻意不含 uid：身份在目标重新取得）。"""
    return {
        "name": e.get("name") or "",
        "earliest_start": e.get("earliest_start"),
        "latest_start": e.get("latest_start"),
        "duration_minutes": e.get("duration_minutes"),
        "note": e.get("note") or "",
        "timezone": e.get("timezone"),
    }


def _event_view(e, compute_id, include_uid):
    view = {"compute_id": compute_id}
    if include_uid:
        # 目标事件携带持续身份；复制事件刻意不带 uid 键——其身份在目标
        # 落库时重新分配（新持续身份）。
        view["uid"] = e.get("uid")
    view.update(_event_fields(e))
    return view


def _resolve_selection(source_events, selected_keys):
    """解析入选事件，返回按来源快照顺序排列的入选事件列表。

    selected_keys 为 None 表示全选；否则必须是非空字符串数组、不得重复，
    且每个 key 都必须存在于来源快照中，否则抛 InvalidMergeSelection。
    """
    all_keys = [e.get("key") for e in source_events]
    by_key = {e.get("key"): e for e in source_events}
    if selected_keys is None:
        chosen = list(source_events)
    else:
        if not isinstance(selected_keys, list) or selected_keys == []:
            raise InvalidMergeSelection("请至少选择一个要复制的事件")
        if any(not isinstance(k, str) or not k for k in selected_keys):
            raise InvalidMergeSelection("event_keys 必须是非空事件 key 字符串数组")
        if len(set(selected_keys)) != len(selected_keys):
            raise InvalidMergeSelection("event_keys 含重复的事件 key")
        missing = [k for k in selected_keys if k not in by_key]
        if missing:
            raise InvalidMergeSelection(
                "所选事件在来源版本中不存在：{}".format("、".join(missing[:5])))
        chosen = [by_key[k] for k in all_keys if k in set(selected_keys)]
    if not chosen:
        raise InvalidMergeSelection("来源版本没有可并入的事件")
    return chosen


def build_merge_plan(target, source, selected_keys=None, new_key=None):
    """构建合并计划（不读写数据库）。

    target/source 结构：
      target = {"title", "version",
                "input": {"events": [快照式事件], "relations": [快照式关系]}}
      source = {"id", "title", "version", "input": {... 同上 ...}}
    快照式事件含 key/uid/name/earliest_start/latest_start/duration_minutes/
    note/timezone；快照式关系以 predecessor_key/successor_key 引用并带
    min_gap_minutes/max_gap_minutes。

    返回计划 dict：
      merged_events/merged_relations  落库用的 key 形式输入
                                       （目标事件沿用 key/uid，复制事件为
                                        新临时 key 且不带 uid）
      events.target/copied            预览用事件视图（compute_id = 序号）
      relations.target/copied/skipped 预览用关系视图（skipped 带原因）
      result                          合并后的完整推算结果（id 为序号）
      signature                       推算结果的规范化签名
      counts / selected_keys
    """
    if new_key is None:
        new_key = default_new_key

    t_input = target.get("input") or {}
    s_input = source.get("input") or {}
    t_events = list(t_input.get("events") or [])
    t_relations = list(t_input.get("relations") or [])
    s_events = list(s_input.get("events") or [])
    s_relations = list(s_input.get("relations") or [])

    chosen = _resolve_selection(s_events, selected_keys)
    chosen_keys = {e["key"] for e in chosen}
    s_by_key = {e.get("key"): e for e in s_events}

    # ---- 合并后的事件（key 形式）：目标在前、复制在后 -------------------
    merged_events = []
    for e in t_events:  # 目标事件：key 与 uid 原样保留
        merged_events.append(dict(e))
    copied_save = []
    for e in chosen:  # 复制事件：新临时 key、不带 uid（新身份由 db 分配）
        item = {"key": new_key()}
        item.update(_event_fields(e))
        copied_save.append(item)
        merged_events.append(item)

    ord_by_key = {e["key"]: i for i, e in enumerate(merged_events)}

    # ---- 来源关系：仅两端均入选才复制（等待上下界原样保留） -------------
    # 来源事件 key → 复制事件新临时 key
    new_key_by_source_key = {e_src["key"]: e_new["key"]
                             for e_src, e_new in zip(chosen, copied_save)}

    copied_save_relations = []
    copied_relations_view = []
    skipped_relations_view = []
    for r in s_relations:
        pkey, skey = r.get("predecessor_key"), r.get("successor_key")
        min_gap, max_gap = _gap_pair(r)
        p_event, s_event = s_by_key.get(pkey), s_by_key.get(skey)
        if p_event is None or s_event is None:
            skipped_relations_view.append({
                "predecessor": {"name": p_event["name"] if p_event else None,
                                "compute_id": None,
                                "selected": p_event is not None and pkey in chosen_keys},
                "successor": {"name": s_event["name"] if s_event else None,
                              "compute_id": None,
                              "selected": s_event is not None and skey in chosen_keys},
                "min_gap_minutes": min_gap, "max_gap_minutes": max_gap,
                "reason": "关系引用了来源快照中不存在的事件，不予复制",
            })
            continue
        if pkey in chosen_keys and skey in chosen_keys:
            np_key, ns_key = new_key_by_source_key[pkey], new_key_by_source_key[skey]
            copied_save_relations.append({
                "predecessor_key": np_key, "successor_key": ns_key,
                "min_gap_minutes": min_gap, "max_gap_minutes": max_gap,
            })
            copied_relations_view.append({
                "predecessor": {"compute_id": ord_by_key[np_key], "name": p_event["name"]},
                "successor": {"compute_id": ord_by_key[ns_key], "name": s_event["name"]},
                "min_gap_minutes": min_gap, "max_gap_minutes": max_gap,
            })
        else:
            unselected = [e["name"] for key, e in ((pkey, p_event), (skey, s_event))
                          if key not in chosen_keys]
            skipped_relations_view.append({
                "predecessor": {"name": p_event["name"], "compute_id": None,
                                "selected": pkey in chosen_keys},
                "successor": {"name": s_event["name"], "compute_id": None,
                              "selected": skey in chosen_keys},
                "min_gap_minutes": min_gap, "max_gap_minutes": max_gap,
                "reason": "仅复制两端事件均入选的关系；事件「{}」未入选，该关系不复制".format(
                    "、".join(unselected)),
            })

    # 合并后的关系（key 形式）：目标关系在前、复制关系在后
    merged_relations = [{
        "predecessor_key": r.get("predecessor_key"),
        "successor_key": r.get("successor_key"),
        "min_gap_minutes": (r.get("min_gap_minutes") or 0),
        "max_gap_minutes": r.get("max_gap_minutes"),
    } for r in t_relations] + copied_save_relations

    # ---- 按序号编号的推算输入与结果 --------------------------------------
    compute_events = [{**e, "id": i} for i, e in enumerate(merged_events)]
    compute_relations = [{
        "id": i,
        "predecessor_id": ord_by_key[r["predecessor_key"]],
        "successor_id": ord_by_key[r["successor_key"]],
        "min_gap_minutes": r["min_gap_minutes"],
        "max_gap_minutes": r["max_gap_minutes"],
    } for i, r in enumerate(merged_relations)]
    result = compute(compute_events, compute_relations)
    signature = canonical_signature(result)

    target_view = [_event_view(e, i, include_uid=True)
                   for i, e in enumerate(t_events)]
    copied_view = [_event_view(e, len(t_events) + idx, include_uid=False)
                   for idx, e in enumerate(chosen)]
    t_by_key = {e.get("key"): e for e in t_events}
    target_relations_view = [{
        "predecessor": {"compute_id": ord_by_key[r["predecessor_key"]],
                        "name": t_by_key[r["predecessor_key"]]["name"]},
        "successor": {"compute_id": ord_by_key[r["successor_key"]],
                      "name": t_by_key[r["successor_key"]]["name"]},
        "min_gap_minutes": r["min_gap_minutes"],
        "max_gap_minutes": r["max_gap_minutes"],
    } for r in merged_relations[:len(t_relations)]]

    return {
        "merged_events": merged_events,
        "merged_relations": merged_relations,
        "events": {"target": target_view, "copied": copied_view},
        "relations": {"target": target_relations_view,
                      "copied": copied_relations_view,
                      "skipped": skipped_relations_view},
        "result": result,
        "signature": signature,
        "selected_keys": [e["key"] for e in chosen],
        "counts": {
            "source_events_total": len(s_events),
            "target_events": len(t_events),
            "copied_events": len(chosen),
            "target_relations": len(t_relations),
            "copied_relations": len(copied_relations_view),
            "skipped_relations": len(skipped_relations_view),
        },
    }


def canonical_signature(result):
    """推算结果的规范化序列化签名。

    计划中窗口/冲突/暂存的事件 id 已是合并序号（整数），直接按 key 排序
    序列化即可；预览与确认对同一份目标输入与同一组入选事件重算，结果与
    签名必须逐字节一致。
    """
    return json.dumps(result, ensure_ascii=False, sort_keys=True)
