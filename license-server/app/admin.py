"""授權管理 CLI（給賣方 / 你自己用）

  python -m app.admin keygen                                   # 產生簽章金鑰對（只需一次）
  python -m app.admin issue --customer "ABC Corp" --edition professional --days 365
  python -m app.admin issue --customer "銀行A" --edition enterprise --days 365 --offline -o abc.lic
  python -m app.admin list
  python -m app.admin show ST-PRO-2026-00001
  python -m app.admin revoke ST-PRO-2026-00001 --reason "未續約"
  python -m app.admin inspect <license key 或 .lic 檔>        # 解開並驗證任何 token

環境變數：
  LICENSE_PRIVATE_KEY  私鑰檔路徑（預設 ./secrets/license_private.pem）
  LICENSE_DB           SQLite 路徑（預設 ./data/licenses.db）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import tokens
from .core import LicenseError, LicenseService
from .editions import ALL_FEATURES, EDITIONS

KEY_PATH = Path(os.getenv("LICENSE_PRIVATE_KEY", "secrets/license_private.pem"))
DB_PATH = Path(os.getenv("LICENSE_DB", "data/licenses.db"))


def _service() -> LicenseService:
    if not KEY_PATH.exists():
        sys.exit(f"找不到私鑰 {KEY_PATH}，請先執行：python -m app.admin keygen")
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return LicenseService(DB_PATH, tokens.load_private_key(KEY_PATH))


def cmd_keygen(a) -> int:
    if KEY_PATH.exists() and not a.force:
        sys.exit(f"{KEY_PATH} 已存在。重新產生會讓所有已簽發的授權失效；確定要換請加 --force")
    priv, pub = tokens.generate_keypair()
    tokens.save_private_key(priv, KEY_PATH)
    print(f"✅ 私鑰已存到 {KEY_PATH}（權限 600）。請備份到安全的地方，絕對不要 commit 或交給客戶。\n")
    print("把下面這行公鑰貼進 rca-engine/app/license_keys.py 的 VENDOR_PUBLIC_KEYS：\n")
    print(f'    "{pub}",')
    return 0


def cmd_issue(a) -> int:
    svc = _service()
    try:
        res = svc.issue(a.customer, a.edition, days=a.days, expires_at=a.expires_at,
                        max_services=a.max_services, max_nodes=a.max_nodes, max_activations=a.max_activations,
                        extra_features=a.extra_feature, grace_days=a.grace_days, offline=a.offline,
                        notes=a.notes or "")
    except LicenseError as e:
        sys.exit(f"❌ {e.message}")
    p = res["payload"]
    print(f"✅ 已簽發 {res['license_id']}｜{p['customer']}｜{p['edition']}｜到期 {p['expires_at']}"
          f"｜服務上限 {p['max_services'] or '不限'}｜啟用台數 {p['max_activations']}｜"
          f"{'離線授權' if p['offline'] else '線上啟用'}")
    print(f"功能：{', '.join(p['features'])}\n")
    if a.output:
        Path(a.output).write_text(res["license_key"] + "\n", encoding="utf-8")
        print(f"License 檔已寫入 {a.output}（交給客戶放到 data/license.key）")
    else:
        print("License Key（交給客戶設定在 LICENSE_KEY 或 data/license.key）：\n")
        print(res["license_key"])
    return 0


def cmd_list(a) -> int:
    rows = _service().list_licenses()
    print(f"{'ID':<22}{'客戶':<20}{'方案':<14}{'到期':<23}{'狀態':<9}啟用")
    for r in rows:
        print(f"{r['license_id']:<22}{r['customer'][:18]:<20}{r['edition']:<14}{r['expires_at']:<23}"
              f"{r['status']:<9}{r['active_activations']}/{r['max_activations']}")
    return 0


def cmd_show(a) -> int:
    svc = _service()
    try:
        d = svc.get(a.license_id)
    except LicenseError as e:
        sys.exit(f"❌ {e.message}")
    if not a.with_key:
        d.pop("token")
    d["audit"] = svc.audit_log(a.license_id, limit=20)
    print(json.dumps(d, ensure_ascii=False, indent=2))
    return 0


def cmd_revoke(a) -> int:
    try:
        _service().revoke(a.license_id, a.reason or "")
    except LicenseError as e:
        sys.exit(f"❌ {e.message}")
    print(f"✅ 已撤銷 {a.license_id}。線上授權的客戶會在下次續驗時失效（最長等於租期 + 寬限期）。")
    return 0


def cmd_inspect(a) -> int:
    tok = Path(a.token).read_text(encoding="utf-8").strip() if Path(a.token).exists() else a.token
    print(json.dumps(tokens.peek(tok), ensure_ascii=False, indent=2))
    if KEY_PATH.exists():
        try:
            tokens.verify(tok, [tokens.public_key_str(tokens.load_private_key(KEY_PATH))])
            print("\n✅ 簽章有效（由目前的私鑰簽發）")
        except ValueError as e:
            print(f"\n❌ {e}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="license-admin")
    sub = p.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("keygen", help="產生簽章金鑰對")
    k.add_argument("--force", action="store_true")
    k.set_defaults(fn=cmd_keygen)

    i = sub.add_parser("issue", help="簽發授權")
    i.add_argument("--customer", required=True)
    i.add_argument("--edition", required=True, choices=list(EDITIONS))
    i.add_argument("--days", type=int, default=365)
    i.add_argument("--expires-at", help="到期日 ISO 格式，例如 2027-12-31T23:59:59Z（優先於 --days）")
    i.add_argument("--max-services", type=int)
    i.add_argument("--max-nodes", type=int)
    i.add_argument("--max-activations", type=int)
    i.add_argument("--extra-feature", action="append", choices=list(ALL_FEATURES))
    i.add_argument("--grace-days", type=int, default=14)
    i.add_argument("--offline", action="store_true", help="離線授權（客戶不需連線啟用）")
    i.add_argument("--notes")
    i.add_argument("-o", "--output", help="寫成 .lic 檔")
    i.set_defaults(fn=cmd_issue)

    sub.add_parser("list", help="列出授權").set_defaults(fn=cmd_list)

    s = sub.add_parser("show", help="授權詳情 + 啟用紀錄 + 稽核紀錄")
    s.add_argument("license_id")
    s.add_argument("--with-key", action="store_true")
    s.set_defaults(fn=cmd_show)

    r = sub.add_parser("revoke", help="撤銷授權")
    r.add_argument("license_id")
    r.add_argument("--reason")
    r.set_defaults(fn=cmd_revoke)

    n = sub.add_parser("inspect", help="解開 token")
    n.add_argument("token")
    n.set_defaults(fn=cmd_inspect)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
