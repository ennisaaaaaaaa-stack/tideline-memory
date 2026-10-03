#!/usr/bin/env python3
"""v2.9 感官写口夹具（Phase 18）：器官 → context 层的入境口岸。

背景（spec: ~/drafts/sensory-write-port-spec-v0.md，HN gate「感官写口带质地面」）：
  任何器官（电台耳朵/摄像头/传感器/RSS）的感知经统一信封写入 context 层，
  走既有固化与检索管线，不为每种器官改一次记忆系统。核心原则「入境≠注意」：
  写口唯一在入境处拦截的是 consent，显著性永不拦截——hint 为空的原始流照写。
  质地面纪律：蒸馏信号与原始样本引用成对落地；纹理可以过期，引用不可以悬空。

验收点：
  S0 生产对齐：模块在；server context 表真实 schema 与写口 INSERT 列一致
  S1 必填缺失拒收（五个必填字段各验一次，零落行）
  S2 非法 confidence / consent_tier 拒收（越界/类型错/bool 混入）
  S3 consent 三级各走对：tier0 拒写零落行 / tier1 降级去ref记dropped / tier2 全量保ref
  S4 saliency_hint 为空可写（未过滤原始流照写）
  S5 全量写入后 payload_ref 可从库里取回（质地面回头路走得通）
  S6 孤儿检测：文件在=非孤儿，悬空=孤儿
  S7 回执字段齐（context_id / action / summary）
  S8 自定义模态策略生效 + 未知模态 fail-closed

跑法: /home/ubuntu/.hermes/hermes-agent/venv/bin/python tests/fixture_phase18_sensory_write.py
"""
import importlib.util, json, os, shutil, sqlite3, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
SERVER = REPO / "server.py"
SENSORY = REPO / "sensory_write.py"
assert SERVER.exists(), f"server.py not found at {SERVER}"

results = []

def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))

# ── S0a 模块存在（RED 阶段此条先红）──
check("S0a sensory_write.py 存在", SENSORY.exists(), str(SENSORY))
if not SENSORY.exists():
    print()
    print(f"💥 1/1 passed — 模块未创建（RED）")
    sys.exit(1)

spec = importlib.util.spec_from_file_location("sensory_write", str(SENSORY))
sw = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sw)

DB = tempfile.mktemp(suffix=".db")
os.environ["MEMORY_MCP_DB"] = DB
os.environ["EMBEDDING_API_KEY"] = ""      # local 路模式，不走向量化

SAMPLES = tempfile.mkdtemp(prefix="sensory_samples_")

VALID_REF = "raw/20261001T0214/audio_98.5_1732s.wav"

def ev(**kw):
    """一条合法的电台耳朵事件，可覆写任意字段。"""
    e = {
        "ts": "2026-10-01T02:14:00Z",
        "organ_id": "radio-ear-01",
        "modality": "audio",
        "event_type": "song_detected",
        "payload_summary": "《海阔天空》副歌段，FM 98.5",
        "payload_ref": VALID_REF,
        "confidence": 0.87,
        "consent_tier": 2,
        "saliency_hint": 0.42,
    }
    e.update(kw)
    return e

def load_server():
    sspec = importlib.util.spec_from_file_location("memory_server_p18", str(SERVER))
    srv = importlib.util.module_from_spec(sspec)
    sspec.loader.exec_module(srv)
    return srv

def main():
    print("═══ Phase 18: sensory_write 感官写口 ═══")

    # 用真实 server._init() 建库——生产 schema，不是夹具私有的简化表
    srv = load_server()
    srv._init()
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row

    # ── S0 生产对齐 ──
    print("── S0 生产对齐 ──")
    cols = [r["name"] for r in c.execute("PRAGMA table_info(context)").fetchall()]
    need = {"content", "embedding", "meta", "created_at"}
    check("S0b context表真实schema含写口所需列", need <= set(cols), ",".join(cols))
    has_all = (hasattr(sw, "ingest") and hasattr(sw, "consent_decide")
               and hasattr(sw, "validate_envelope") and hasattr(sw, "check_orphans"))
    check("S0c 四个公开函数齐(validate/decide/ingest/orphans)", has_all)

    # ── S1 必填缺失拒收 ──
    print("── S1 必填缺失拒收 ──")
    for f in ("ts", "organ_id", "modality", "event_type", "payload_summary"):
        bad = ev(); del bad[f]
        r = sw.ingest(c, bad)
        check(f"S1-{f} 缺失拒收", (not r["ok"]) and r["action"] == "invalid",
              f"action={r.get('action')}")
    n = c.execute("SELECT COUNT(*) n FROM context").fetchone()["n"]
    check("S1f 校验失败零落行", n == 0, f"rows={n}")

    # ── S2 非法数值拒收 ──
    print("── S2 非法 confidence/consent_tier 拒收 ──")
    for label, bad in [
        ("confidence>1", ev(confidence=1.5)),
        ("confidence<0", ev(confidence=-0.1)),
        ("confidence字符串", ev(confidence="high")),
        ("confidence布尔", ev(confidence=True)),
        ("confidence缺失", ev(confidence=None)),
        ("tier负数", ev(consent_tier=-1)),
        ("tier小数", ev(consent_tier=2.5)),
        ("tier字符串", ev(consent_tier="2")),
        ("tier布尔", ev(consent_tier=True)),
        ("hint越界", ev(saliency_hint=1.2)),
    ]:
        r = sw.ingest(c, bad)
        check(f"S2-{label} 拒收", (not r["ok"]) and r["action"] == "invalid",
              f"action={r.get('action')}")
    n = c.execute("SELECT COUNT(*) n FROM context").fetchone()["n"]
    check("S2k 非法值零落行", n == 0, f"rows={n}")

    # ── S3 consent 执法（设计师拍板 2026-10-02：默认全模态最低档，原始永不落盘） ──
    print("── S3 consent 执法：默认最低档 + 显式策略全量 ──")
    d0 = sw.consent_decide(ev(consent_tier=0), None)
    d1 = sw.consent_decide(ev(consent_tier=1), None)
    d2 = sw.consent_decide(ev(consent_tier=2), None)
    d3 = sw.consent_decide(ev(consent_tier=3), None)
    check("S3a decide默认档 tier0=reject", d0 == "reject", d0)
    check("S3b decide默认档 tier1=write_degraded", d1 == "write_degraded", d1)
    check("S3c decide默认档 tier2=write_degraded（拍板：原始样本永不落盘）",
          d2 == "write_degraded", d2)
    check("S3c+ decide默认档 tier3=write_degraded（999不可达=永不全量）",
          d3 == "write_degraded", d3)

    # tier0：拒写且零落行
    r0 = sw.ingest(c, ev(consent_tier=0, payload_summary="低授权样本"))
    n0 = c.execute("SELECT COUNT(*) n FROM context").fetchone()["n"]
    check("S3d tier0拒写回执", (not r0["ok"]) and r0["action"] == "reject",
          f"action={r0.get('action')}")
    check("S3e tier0零落行", n0 == 0, f"rows={n0}")

    # tier1：降级——丢 payload_ref，记 original_ref_dropped，信号本身照写
    r1 = sw.ingest(c, ev(consent_tier=1, payload_summary="降级样本·只存特征"),
                   now="2026-10-01 02:20:00")
    check("S3f tier1降级回执", r1["ok"] and r1["action"] == "write_degraded",
          f"action={r1.get('action')}")
    row1 = c.execute("SELECT * FROM context WHERE id=?", (r1["context_id"],)).fetchone()
    m1 = json.loads(row1["meta"]) if row1 else {}
    check("S3g 降级行payload_ref已去", row1 is not None and m1.get("payload_ref") is None,
          str(m1.get("payload_ref")))
    check("S3h 降级记original_ref_dropped=true", m1.get("original_ref_dropped") is True,
          str(m1.get("original_ref_dropped")))
    check("S3i 降级丢的是纹理不是信号(content=summary)",
          row1 is not None and row1["content"] == "降级样本·只存特征", row1["content"] if row1 else "-")
    check("S3j 行meta带type=sensory+action_taken", m1.get("type") == "sensory"
          and m1.get("action_taken") == "write_degraded",
          f"type={m1.get('type')} action_taken={m1.get('action_taken')}")

    # tier2（默认策略）：同降级——原始永不落盘是拍板行为，不是漏做
    r2 = sw.ingest(c, ev(consent_tier=2, payload_summary="默认档tier2样本"),
                   now="2026-10-01 02:21:00")
    check("S3k 默认档tier2也降级（永不存原始）", r2["ok"] and r2["action"] == "write_degraded",
          f"action={r2.get('action')}")
    check("S3l 默认档tier2回执无ref", r2.get("payload_ref") is None, str(r2.get("payload_ref")))

    # 显式策略下全量仍可达（「器官单独点头」路径，设计师拍板预留）
    r2f = sw.ingest(c, ev(consent_tier=2, payload_summary="显式策略全量样本"),
                    policy={"audio": {"min_degraded": 1, "min_tier_full": 2}},
                    now="2026-10-01 02:22:00")
    check("S3m 显式策略audio tier2=write_full", r2f["ok"] and r2f["action"] == "write_full",
          f"action={r2f.get('action')}")
    check("S3n 显式策略回执带原ref", r2f.get("payload_ref") == VALID_REF, str(r2f.get("payload_ref")))

    # ── S4 hint 为空可写 ──
    print("── S4 hint 为空可写（入境≠注意）──")
    n_before = c.execute("SELECT COUNT(*) n FROM context").fetchone()["n"]
    nohint = ev(); del nohint["saliency_hint"]
    r4 = sw.ingest(c, nohint)
    n4 = c.execute("SELECT COUNT(*) n FROM context").fetchone()["n"]
    check("S4a 无hint可写（默认档降级落行）", r4["ok"] and r4["action"] == "write_degraded"
          and n4 == n_before + 1,
          f"action={r4.get('action')} rows={n_before}->{n4}")
    row4 = c.execute("SELECT meta FROM context WHERE id=?", (r4["context_id"],)).fetchone()
    m4 = json.loads(row4["meta"])
    check("S4b 行meta记saliency_hint=null", m4.get("saliency_hint") is None,
          str(m4.get("saliency_hint")))
    # hint=0 是合法值（显式评过分为零），区别于 null（未过滤）
    r4b = sw.ingest(c, ev(saliency_hint=0.0, payload_summary="显式零分"))
    check("S4c hint=0.0合法且保留", r4b["ok"] and r4b.get("saliency_hint") == 0.0,
          str(r4b.get("saliency_hint")))

    # ── S5 质地面回头路：显式策略全量写入后 ref 可从库里取回 ──
    print("── S5 payload_ref 库内可取回（走S3m显式策略样本） ──")
    row2 = c.execute("SELECT * FROM context WHERE id=?", (r2f["context_id"],)).fetchone()
    m2 = json.loads(row2["meta"])
    check("S5a 全量行meta.payload_ref==原引用", m2.get("payload_ref") == VALID_REF,
          str(m2.get("payload_ref")))
    check("S5b 行meta带信封字段(ts/organ/modality/event_type)", m2.get("ts") == "2026-10-01T02:14:00Z" and m2.get("organ_id") == "radio-ear-01"
          and m2.get("modality") == "audio" and m2.get("event_type") == "song_detected",
          f"{m2.get('ts')}|{m2.get('organ_id')}|{m2.get('modality')}")
    check("S5c created_at用server同款格式", row2["created_at"] == "2026-10-01 02:22:00",
          row2["created_at"])

    # ── S6 孤儿检测 ──
    print("── S6 孤儿检测（引用不可以悬空）──")
    os.makedirs(os.path.join(SAMPLES, "raw", "20261001T0214"), exist_ok=True)
    open(os.path.join(SAMPLES, VALID_REF), "wb").write(b"RIFF-fake-audio")
    # refA：文件在（显式策略全量写入）；refB：悬空（显式策略全量写入，文件不存在）
    FULL_POLICY = {"audio": {"min_degraded": 1, "min_tier_full": 2}}
    rA = sw.ingest(c, ev(payload_ref=VALID_REF, payload_summary="样本A·文件在"),
                   policy=FULL_POLICY, now="2026-10-01 03:00:00")
    rB = sw.ingest(c, ev(payload_ref="raw/20261001T0300/gone.wav", payload_summary="样本B·文件丢"),
                   policy=FULL_POLICY, now="2026-10-01 03:01:00")
    orphans = sw.check_orphans(c, SAMPLES)
    orphan_ids = [oid for oid, _ in orphans]
    check("S6a 悬空引用报孤儿", (rB["context_id"], "raw/20261001T0300/gone.wav") in orphans,
          str(orphans))
    check("S6b 文件在的不报", rA["context_id"] not in orphan_ids, str(orphan_ids))
    check("S6c 降级行(无ref)不算孤儿", r1["context_id"] not in orphan_ids, str(orphan_ids))

    # ── S7 回执字段齐 ──
    print("── S7 回执字段齐 ──")
    r7 = sw.ingest(c, ev(payload_summary="回执字段检查"), now="2026-10-01 04:00:00")
    keys = {"ok", "context_id", "action", "summary"}
    check("S7a 回执含 context_id/action/summary", keys <= set(r7.keys()),
          ",".join(sorted(r7.keys())))
    check("S7b context_id指向真实行",
          r7["context_id"] is not None
          and c.execute("SELECT content FROM context WHERE id=?",
                        (r7["context_id"],)).fetchone() is not None,
          f"context_id={r7.get('context_id')}")
    check("S7c 回执summary==payload_summary", r7["summary"] == "回执字段检查", r7.get("summary", "-"))

    # ── S8 自定义策略 + fail-closed ──
    print("── S8 自定义模态策略 ──")
    p8 = {"text": {"min_degraded": 0, "min_tier_full": 0}}
    r8 = sw.ingest(c, ev(modality="text", consent_tier=0, payload_ref=None,
                         payload_summary="文本器官·tier0全量"), policy=p8)
    check("S8a 自定义策略text tier0=全量", r8["ok"] and r8["action"] == "write_full",
          f"action={r8.get('action')}")
    d8 = sw.consent_decide(ev(modality="audio"), {"text": {"min_degraded": 0, "min_tier_full": 0}})
    check("S8b 自定义策略缺模态且无*兜底→fail-closed拒", d8 == "reject", d8)

    c.close()
    print()
    failed = [n for n, ok in results if not ok]
    print(f"{'🎉' if not failed else '💥'} {len(results)-len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)

try:
    main()
finally:
    for p in (DB, DB + "-wal", DB + "-shm"):
        if os.path.exists(p):
            os.unlink(p)
    shutil.rmtree(SAMPLES, ignore_errors=True)
