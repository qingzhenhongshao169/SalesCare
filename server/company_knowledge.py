"""Source-backed Biaofun tenant package. Import never replaces existing tenants."""
import json
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1] / "knowledge" / "biaofun.json"


def apply_company(d):
    package = json.loads(PACKAGE.read_text(encoding="utf-8"))
    d.update(id="biaofun", name="标梵互动", industry="品牌 GEO · 高端建站 · 软件研发 · 数字营销",
             tagline="以技术为引擎，助力客户业绩增长。", conversations=[], tasks=[], gaps=[], audit=[],
             company_website="https://www.biaofun.com/", knowledge_package=package["version"])
    for m in d["members"]:
        m["skill"] = "数字营销与开发咨询"
    d["offers"] = package["offers"]
    d["knowledge"] = package["entries"]
    return d
