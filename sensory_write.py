#!/usr/bin/env python3
"""sensory_write — 感官写口：器官 → context 层的入境口岸（Phase 18）。

spec: ~/drafts/sensory-write-port-spec-v0.md
定位：任何器官（电台耳朵/摄像头/传感器/RSS）的感知经统一信封写入 context 层，
走既有固化与检索管线，记忆系统不为器官改表。

核心原则「入境 ≠ 注意」：
  写口唯一在入境处拦截的是 consent；显著性永不拦截——saliency_hint 为空的
  未过滤原始流照写，hint 只影响 Layer 0 固化的提升优先级，不影响是否入库。

质地面（texture plane）纪律：
  payload_summary（信号）与 payload_ref（原始样本引用）成对落地；
  样本库有保留策略，纹理可以过期，引用不可以悬空——check_orphans 报不变量违反。

生产对齐点（接线人必读）：
  1. context 表 schema 取自 server.py:110 —— (id, content, embedding, meta,
     created_at)，零 schema 改动（spec §6）：ts/organ/modality 等信封字段全部
     进 meta JSON（meta 本来就是自由 JSON），type='sensory' 是唯一约定。
  2. embedding 列写 NULL——向量化是既有管线的事（context_record 的 _embed /
     scan_unindexed），写口保持同步、薄，不重复造轮子。
  3. created_at 与 server._now() 同格式：UTC "%Y-%m-%d %H:%M:%S"。
  4. DEFAULT_POLICY 档位是占位默认——【待设计师拍板】（spec §7 开放问题 3：
     consent_tier 分几级、每级对应什么数据去向）。

用法：
    from sensory_write import ingest, check_orphans
    receipt = ingest(conn, envelope)                  # 校验→执法→落行
    orphans = check_orphans(conn, samples_root)       # 体检：悬空引用清单
"""
import json
import os
from datetime import datetime, timezone

__all__ = [
    "validate_envelope", "consent_decide", "ingest", "check_orphans",
    "DEFAULT_POLICY",
]

# ─── consent 策略 ────────────────────────────────────────────────────────
# 语义（每模态一档）：
#   min_degraded  : 低于此 tier → 'reject'（拒写，唯一在写口拦截的情况）
#   min_tier_full : 低于此 tier → 'write_degraded'（丢 payload_ref 保信号）
#                   达到       → 'write_full'（信号+纹理全量落）
#
# 【设计师拍板 2026-10-02】默认全模态最低档：只留蒸馏信号，原始样本永不落盘。
# 原话：「最低档授权就够了，不然太占空间了吧？器官毕竟是没有经过真实判断筛选的内容。」
# 想存原始样本的器官，接入时单独传 policy（把 min_tier_full 设为可达档位）。
# tier 尺度：0=无授权 1=仅蒸馏信号 2=可存原始样本 3=可参与 DREAM
# "*" = 全模态兜底：tier0(无授权) 拒写；tier1+ 一律降级；999 不可达 = 永不全量。
DEFAULT_POLICY = {
    "*": {"min_degraded": 1, "min_tier_full": 999},
}

REQUIRED_FIELDS = ("ts", "organ_id", "modality", "event_type", "payload_summary")


def validate_envelope(evt):
    """校验感官事件信封。返回 (ok, errors)；errors 为字符串清单，ok 时为空。

    规则：
      必填：ts / organ_id / modality / event_type / payload_summary，非空字符串
      confidence    : 必填，数值（非 bool），∈[0, 1]
      consent_tier  : 必填，整数（非 bool），≥ 0
      saliency_hint : 可空；非空时数值 ∈[0, 1]（hint=0.0 合法=显式评过分为零）
      payload_ref   : 可空；非空时字符串
      meta          : 可选 dict（器官自定义元数据）
    """
    errors = []
    if not isinstance(evt, dict):
        return False, ["envelope 必须是 dict"]

    for f in REQUIRED_FIELDS:
        v = evt.get(f)
        if not isinstance(v, str) or not v.strip():
            errors.append(f"必填字段缺失或为空: {f}")

    conf = evt.get("confidence")
    if not isinstance(conf, (int, float)) or isinstance(conf, bool):
        errors.append("confidence 必须是数值（非 bool）")
    elif not (0.0 <= conf <= 1.0):
        errors.append(f"confidence 越界: {conf}（须 ∈[0,1]）")

    tier = evt.get("consent_tier")
    if not isinstance(tier, int) or isinstance(tier, bool):
        errors.append("consent_tier 必须是整数（非 bool）")
    elif tier < 0:
        errors.append(f"consent_tier 为负: {tier}")

    hint = evt.get("saliency_hint")  # 可空
    if hint is not None:
        if not isinstance(hint, (int, float)) or isinstance(hint, bool):
            errors.append("saliency_hint 非空时必须是数值")
        elif not (0.0 <= hint <= 1.0):
            errors.append(f"saliency_hint 越界: {hint}（须 ∈[0,1]）")

    ref = evt.get("payload_ref")  # 可空
    if ref is not None and not isinstance(ref, str):
        errors.append("payload_ref 非空时必须是字符串")

    meta = evt.get("meta")
    if meta is not None and not isinstance(meta, dict):
        errors.append("meta 必须是 dict")

    return (not errors), errors


def consent_decide(evt, policy=None):
    """consent 执法：返回 'reject' | 'write_degraded' | 'write_full'。

    入境 ≠ 注意：显著性不参与此判定；consent 是唯一在写口拦截的维度。
    策略为「每模态一档」的 dict；未配置的模态 fail-closed 直接 reject
    （新器官接入时漏配策略 → 宁可拒写，不可默写）。策略里可用 "*" 键显式
    声明兜底档；没有 "*" 就是全模态白名单语义。
    """
    policy = policy if policy is not None else DEFAULT_POLICY
    modality = evt.get("modality")
    rule = policy.get(modality) or policy.get("*")
    if not rule:
        return "reject"  # fail-closed：未知模态不默写
    tier = evt.get("consent_tier", -1)
    if tier < rule.get("min_degraded", 0):
        return "reject"
    if tier < rule.get("min_tier_full", 0):
        return "write_degraded"
    return "write_full"


def _now_str():
    """与 server._now() 同款：UTC '%Y-%m-%d %H:%M:%S'。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def ingest(conn, evt, policy=None, now=None):
    """感官事件入境：校验 → consent 执法 → INSERT 进 context 表。

    返回回执 dict：
      ok         : bool（write_full / write_degraded 为 True）
      action     : 'invalid' | 'reject' | 'write_degraded' | 'write_full'
      context_id : 落行 id（invalid/reject 时为 None，零落行）
      summary    : payload_summary（invalid 时可能缺）
      payload_ref: 实际落库的 ref（降级时为 None）
      saliency_hint / errors 视情况附带
    """
    ok, errors = validate_envelope(evt)
    if not ok:
        return {"ok": False, "action": "invalid", "context_id": None,
                "errors": errors}

    action = consent_decide(evt, policy)
    if action == "reject":
        return {"ok": False, "action": "reject", "context_id": None,
                "summary": evt.get("payload_summary")}

    ref = evt.get("payload_ref")
    row_meta = dict(evt.get("meta") or {})   # 器官自定义元数据先进
    row_meta.update({                         # 写口字段保护性覆盖
        "type": "sensory",
        "ts": evt["ts"],
        "organ_id": evt["organ_id"],
        "modality": evt["modality"],
        "event_type": evt["event_type"],
        "confidence": evt["confidence"],
        "consent_tier": evt["consent_tier"],
        "saliency_hint": evt.get("saliency_hint"),  # None=未过滤原始流，照写
        "action_taken": action,
    })
    if action == "write_degraded":
        # 降级：丢纹理（payload_ref）保信号——但要在 meta 里留下「丢过」的痕迹
        row_meta["payload_ref"] = None
        row_meta["original_ref_dropped"] = bool(ref)
    else:
        row_meta["payload_ref"] = ref

    created_at = now if now is not None else _now_str()
    cur = conn.execute(
        "INSERT INTO context(content, embedding, meta, created_at) VALUES(?,?,?,?)",
        (evt["payload_summary"], None, json.dumps(row_meta, ensure_ascii=False),
         created_at),
    )
    conn.commit()
    return {
        "ok": True,
        "action": action,
        "context_id": cur.lastrowid,
        "summary": evt["payload_summary"],
        "payload_ref": row_meta["payload_ref"],
        "saliency_hint": row_meta["saliency_hint"],
    }


def check_orphans(conn, samples_root):
    """体检：扫描全部 sensory context 行，报悬空的 payload_ref。

    语义（spec §3 原话）：纹理可以过期，引用不可以悬空。
    样本库有保留策略（按 consent_tier/模态分级），样本文件过期删除是正常
    运维；但 context 行还挂着已不存在的 ref = 不变量违反，报孤儿清单。

    返回 [(context_id, payload_ref)]。
    """
    orphans = []
    for row in conn.execute("SELECT id, meta FROM context").fetchall():
        try:
            meta = json.loads(row["meta"] or "{}")
        except (ValueError, TypeError):
            continue
        if meta.get("type") != "sensory":
            continue
        ref = meta.get("payload_ref")
        if not ref:
            continue  # 降级行本来就没存 ref，无引用即无悬空
        path = os.path.join(samples_root, ref)
        if not os.path.exists(path):
            orphans.append((row["id"], ref))
    return orphans
