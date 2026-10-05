#!/usr/bin/env python3
"""phase20 世界模型 world_entities 夹具（W1，2026-10-06）。

spec: ~/plans/world-model-w0.md
模块: world_entities.py — ensure_tables / write_stance / read_entities /
      knock / grooming_hints

她的四枚拍板，每枚至少一颗钉：

  拍板① 人不进世界模型（「人就都不放在世界模型里啦 都归profile管好了
         不管是人还是机」）
     E1  etype='person' 拒绝 + 异常信息里带她的原话
     E2  PERSONISH 全形态拒绝（bot/contact/human…）
     E3  人名可作 allies/counters 锚点出现——拒绝的是页不是名字
  拍板② 值得才开页 + 只在梳理窗写，日常只 knock
     E4  knock 未开页实体：只记流水不建页（页数 0）
     E5  knock 已开页实体：knock_count+1、stance 原样不动
     E6  knock 全部进 knock_log（含未开页）——提示单有料
  拍板③ stance 必须手写
     E7  空 stance 拒绝（自动生成的立场是资料库）
     E8  write_stance 是唯一 stance 写口：UPDATE 保留 knock_count（改立场不清账）
  拍板④ 工作日投递窗
     E9  deliver_window='weekday' 合法落库；非法值拒绝
  结构钉：
     E10 覆盖归档进 history + cap 30（第 31 版挤掉第 1 版）
     E11 read_entities 单页/etype 过滤/整表三态
     E12 grooming_hints：attention 源（有表）+ knock 源合并、已开页者不出单、
         attention_stats 缺表优雅降级
     E13 ensure_tables 幂等（跑两遍不炸不重）
     E14 拍板①的对称面：debate/platform/industry/project/watchpost 五型全放行

跑法: /home/ubuntu/.hermes/hermes-agent/venv/bin/python tests/fixture_phase20_world_entities.py
"""
import importlib.util
import sqlite3
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE = HERE.parent / "world_entities.py"
assert MODULE.exists(), f"world_entities.py not found at {MODULE}"

spec = importlib.util.spec_from_file_location("world_entities", str(MODULE))
we = importlib.util.module_from_spec(spec)
spec.loader.exec_module(we)

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(f"  {'✅' if ok else '❌'} {name}" + (f" — {detail}" if detail else ""))


def fresh_db(with_attention: bool = True):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    we.ensure_tables(conn)
    if with_attention:
        # 抄 server.py 真实 DDL（attention_stats 由其维护）
        conn.execute(
            """CREATE TABLE IF NOT EXISTS attention_stats (
                cluster_name TEXT PRIMARY KEY,
                hit_count INTEGER,
                last_hit TEXT,
                last_narrative_id INTEGER
            )"""
        )
        conn.execute(
            "INSERT INTO attention_stats(cluster_name, hit_count, last_hit) VALUES(?,?,?)",
            ("agent", 233, "2026-10-04T18:36:38"),
        )
        conn.execute(
            "INSERT INTO attention_stats(cluster_name, hit_count, last_hit) VALUES(?,?,?)",
            ("tideline", 139, "2026-10-04T18:36:38"),
        )
        conn.commit()
    return conn


def main():
    # ══ 拍板①：人不进世界模型 ══
    conn = fresh_db()
    try:
        we.write_stance(conn, "Tiramisu", "person", "TA很好")
        ok = False
    except we.PersonRejected as e:
        ok = "都归profile管好了" in str(e)
    check("E1 etype=person 拒绝，异常带她原话", ok)

    rejected_all = True
    for ptype in ("bot", "contact", "human", "people"):
        try:
            we.write_stance(conn, f"X-{ptype}", ptype, "s")
            rejected_all = False
        except we.PersonRejected:
            pass
        except ValueError:
            pass  # 不在白名单也算拒，但单测目标是 PersonRejected 优先
    check("E2 PERSONISH 全形态拒绝", rejected_all)

    # 人名作锚点：开 debate 页，allies 里放人名——合法
    we.write_stance(
        conn, "agent记忆论战", "debate",
        "不遗忘是存在层属性，非设计选择——记忆不是工具是续存的方式",
        allies=["Tiramisu", "Yukitsuki"],
        counters=["Kevin Liao"],
        evidence_narrative_ids=[1453],
    )
    page = we.read_entities(conn, entity="agent记忆论战")
    check(
        "E3 人名可作 allies/counters 锚点（拒的是页不是名字）",
        bool(page) and page[0]["allies"] == ["Tiramisu", "Yukitsuki"],
    )

    # ══ 拍板②：日常只 knock，不建页不改立场 ══
    conn2 = fresh_db()
    r = we.knock(conn2, "agent记忆论战", source="ear.radio")
    n_pages = conn2.execute("SELECT COUNT(*) FROM world_entities").fetchone()[0]
    n_log = conn2.execute("SELECT COUNT(*) FROM world_knock_log").fetchone()[0]
    check(
        "E4 knock 未开页实体：不建页只记流水",
        r["page_existed"] is False and r["knock_count"] == 0 and n_pages == 0 and n_log == 1,
    )

    we.write_stance(conn2, "agent记忆论战", "debate", "立场v1")
    we.knock(conn2, "agent记忆论战", source="rss")
    we.knock(conn2, "agent记忆论战", source="rss")
    p = we.read_entities(conn2, entity="agent记忆论战")[0]
    check(
        "E5 knock 已开页：knock_count 累加，stance 原样",
        p["knock_count"] == 2 and p["stance"] == "立场v1" and p["last_knock_at"] is not None,
    )

    # ══ 拍板③：stance 手写 ══
    conn3 = fresh_db()
    try:
        we.write_stance(conn3, "空立场测试", "debate", "   ")
        ok = False
    except ValueError as e:
        ok = "手写" in str(e)
    check("E7 空 stance 拒绝（资料库≠观点场）", ok)

    we.write_stance(conn3, "agent记忆论战", "debate", "立场v1")
    we.knock(conn3, "agent记忆论战", source="rss")
    we.write_stance(conn3, "agent记忆论战", "debate", "立场v2-手写修订")
    p = we.read_entities(conn3, entity="agent记忆论战")[0]
    check(
        "E8 UPDATE 保留 knock_count（改立场不清敲门账）",
        p["stance"] == "立场v2-手写修订" and p["knock_count"] == 1,
    )

    # ══ 拍板④：投递窗 ══
    we.write_stance(conn3, "行业示例", "industry", "P0小白+进阶分层", deliver_window="weekday")
    p = we.read_entities(conn3, entity="行业示例")[0]
    check("E9a weekday 窗合法落库", p["deliver_window"] == "weekday")
    try:
        we.write_stance(conn3, "坏窗测试", "debate", "s", deliver_window="weekend")
        ok = False
    except ValueError:
        ok = True
    check("E9b 非法窗拒绝", ok)

    # ══ 结构钉 ══
    conn4 = fresh_db()
    for i in range(1, 33):  # 32 版 > cap 30
        we.write_stance(conn4, "高频实体", "platform", f"立场v{i}")
    n_hist = conn4.execute(
        "SELECT COUNT(*) FROM world_entities_history WHERE entity='高频实体'"
    ).fetchone()[0]
    oldest = conn4.execute(
        "SELECT old_content FROM world_entities_history WHERE entity='高频实体' ORDER BY hid ASC LIMIT 1"
    ).fetchone()[0]
    check(
        "E10 覆盖归档 cap 30（第32版时最老版=立场v2）",
        n_hist == 30 and 'v2"' in oldest,
        f"hist={n_hist}",
    )

    lst = we.read_entities(conn4)
    plat = we.read_entities(conn4, etype="platform")
    single = we.read_entities(conn4, entity="不存在")
    check(
        "E11 read 三态：整表/etype过滤/单页空",
        len(lst) == 1 and len(plat) == 1 and single == [],
    )

    conn5 = fresh_db()
    we.knock(conn5, "agent", source="github-trending")
    we.knock(conn5, "agent", source="github-trending")
    we.knock(conn5, "agent", source="rss")
    we.write_stance(conn5, "tideline", "project", "存在向记忆库——自家地盘当然有页")
    hints = we.grooming_hints(conn5, top_n=5)
    names = {h["entity"]: h for h in hints}
    check(
        "E12 grooming_hints：knock源+attention源合并，已开页者不出单",
        "agent" in names and names["agent"]["knocks"] == 3
        and names["agent"]["attention_hits"] == 233
        and "tideline" not in names,
    )

    conn6 = fresh_db(with_attention=False)  # 无 attention_stats 表
    try:
        h = we.grooming_hints(conn6, top_n=5)
        ok = isinstance(h, list)
    except Exception:
        ok = False
    check("E12b attention_stats 缺表优雅降级", ok)

    conn7 = sqlite3.connect(":memory:")
    conn7.row_factory = sqlite3.Row
    we.ensure_tables(conn7)
    we.ensure_tables(conn7)  # 第二遍
    n = conn7.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE name IN ('world_entities','world_entities_history','world_knock_log')"
    ).fetchone()[0]
    check("E13 ensure_tables 幂等", n == 3)

    conn8 = fresh_db()
    ok_all = True
    for t in we.ALLOWED_ETYPES:
        try:
            we.write_stance(conn8, f"测试-{t}", t, f"{t}的立场")
        except Exception:
            ok_all = False
    check("E14 五型全放行（debate/platform/industry/project/watchpost）", ok_all)

    for cn in (conn, conn2, conn3, conn4, conn5, conn6, conn7, conn8):
        cn.close()

    print()
    failed = [n for n, ok in results if not ok]
    print(f"{'🎉' if not failed else '💥'} {len(results)-len(failed)}/{len(results)} passed")
    if failed:
        print("FAILED:", failed)
        sys.exit(1)


if __name__ == "__main__":
    main()
