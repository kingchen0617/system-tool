"""命令列工具（不需要 Docker / Prometheus 也能跑）

  python -m app.cli simulate provider-timeout          # 跑單一模擬情境
  python -m app.cli simulate provider-timeout --no-llm # 只用規則式 RCA
  python -m app.cli evaluate                           # 跑全部情境，計算 Top-1 / Top-3 準確率與誤報率
  python -m app.cli detect                             # 對真實 Prometheus/Loki 跑一次偵測
  python -m app.cli license status                     # 查看授權狀態
  python -m app.cli license activate <License Key>     # 設定 License Key 並線上啟用
  python -m app.cli license refresh | deactivate
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from .config import settings
from .engine import Engine
from .notifications import format_message


def _engine(no_llm: bool) -> Engine:
    if no_llm:
        settings.llm_enabled = False
    return Engine(settings, persist=False)


def cmd_simulate(args) -> int:
    eng = _engine(args.no_llm)
    sc = eng.load_scenario(args.scenario)
    print(f"情境：{sc.get('name', args.scenario)}　預期 root cause：{sc.get('expected_root_cause')}\n")
    incs = eng.simulate(sc, notify=False)
    if not incs:
        print("沒有產生事件（全部被判定為正常或已抑制）")
    for inc in incs:
        print(f"[{inc.status}] score={inc.score} {inc.score_breakdown}")
        if inc.rca:
            print(format_message(inc))
            if args.json:
                print(json.dumps(inc.rca.model_dump(mode="json"), ensure_ascii=False, indent=2))
        print()
    return 0


def cmd_evaluate(args) -> int:
    eng = _engine(args.no_llm)
    total = top1 = top3 = fp = expected_quiet = 0
    rows = []
    t0 = time.monotonic()
    for name in eng.list_scenarios():
        sc = eng.load_scenario(name)
        exp = sc.get("expected_root_cause")
        incs = [i for i in eng.simulate(sc, notify=False) if i.status == "incident"]
        if exp in (None, "none"):
            expected_quiet += 1
            ok = not incs
            fp += 0 if ok else 1
            rows.append((name, "none", incs[0].rca.suspected_component if incs else "-", "✅" if ok else "❌ 誤報"))
            continue
        total += 1
        got = incs[0].rca if incs and incs[0].rca else None
        cands = [got.suspected_component, *got.alternatives] if got else []
        ok_set = set(sc.get("acceptable_root_causes") or [exp])
        h1 = bool(cands) and cands[0] in ok_set
        h3 = bool(ok_set & set(cands[:3]))
        if got and sc.get("max_confidence") is not None and got.confidence > float(sc["max_confidence"]):
            h1 = h3 = False  # 模稜兩可的情境卻給出過高信心度 → 視為錯誤
        top1 += h1
        top3 += h3
        rows.append((name, exp, f"{cands[0]} ({int(got.confidence * 100)}%, {got.reasoning_source})" if got else "-",
                     "✅" if h1 else ("🟡 top3" if h3 else "❌")))
    dt = time.monotonic() - t0
    print(f"{'情境':<28}{'預期':<16}{'結果':<34}判定")
    for r in rows:
        print(f"{r[0]:<28}{r[1]:<16}{r[2]:<34}{r[3]}")
    print(f"\nTop-1 準確率：{top1}/{total} = {top1 / max(total, 1):.0%}")
    print(f"Top-3 準確率：{top3}/{total} = {top3 / max(total, 1):.0%}")
    print(f"誤報：{fp}/{expected_quiet}　總耗時 {dt:.1f}s")
    return 0 if top3 / max(total, 1) >= 0.8 and fp == 0 else 1


def cmd_detect(args) -> int:
    eng = _engine(args.no_llm)
    incs = eng.run_cycle(notify=not args.no_notify)
    for s in eng.last_signals:
        flag = "NO DATA" if s.no_data else ("ABNORMAL" if s.abnormal else "ok")
        print(f"{flag:<9} {s.describe()}")
    print(f"\n事件：{[i.id for i in incs]}")
    return 0


def cmd_license(args) -> int:
    eng = Engine(settings, persist=True)
    lm = eng.license_manager
    if args.action == "activate":
        if not args.key:
            print("請提供 License Key")
            return 2
        try:
            lm.set_license_key(args.key)
        except ValueError as e:
            print(f"❌ License Key 無效：{e}")
            return 1
        st = eng.refresh_license()
    elif args.action == "refresh":
        st = eng.refresh_license()
    elif args.action == "deactivate":
        print("已停用本機" if lm.deactivate() else "停用失敗（未設定授權或無法連線授權伺服器）")
        st = eng.apply_license()
    else:
        st = eng.license
    print(f"狀態：{st.status}｜{st.reason}")
    print(f"生效方案：{st.edition}（授權方案 {st.licensed_edition or '-'}）｜客戶：{st.customer or '-'}｜{st.license_id or ''}")
    print(f"到期：{st.expires_at or '-'}｜租期：{st.lease_expires_at or '-'}｜寬限至：{st.grace_until or '-'}")
    print(f"服務上限：{st.max_services or '不限'}｜監控中：{len(eng.monitored_services)}"
          + (f"｜未監控：{', '.join(eng.unmonitored_by_license)}" if eng.unmonitored_by_license else ""))
    print(f"功能：{', '.join(st.features)}")
    print(f"Instance ID：{st.instance_id}")
    for w in st.warnings:
        print(f"⚠ {w}")
    if st.last_error:
        print(f"最近錯誤：{st.last_error}")
    return 0 if st.active else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="system-tool")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("simulate")
    s.add_argument("scenario")
    s.add_argument("--no-llm", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_simulate)
    e = sub.add_parser("evaluate")
    e.add_argument("--no-llm", action="store_true")
    e.set_defaults(fn=cmd_evaluate)
    d = sub.add_parser("detect")
    d.add_argument("--no-llm", action="store_true")
    d.add_argument("--no-notify", action="store_true")
    d.set_defaults(fn=cmd_detect)
    lc = sub.add_parser("license")
    lc.add_argument("action", choices=["status", "activate", "refresh", "deactivate"])
    lc.add_argument("key", nargs="?")
    lc.set_defaults(fn=cmd_license)
    args = p.parse_args(argv)
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
