#!/usr/bin/env python
"""导出朝夕 App 客户端 `/v1` 契约到 OpenAPI snapshot（CONTRACT-001）。

用法::

    .venv/bin/python scripts/export_openapi.py            # 写回默认 snapshot 路径
    .venv/bin/python scripts/export_openapi.py --check    # 只校验，不写文件
    .venv/bin/python scripts/export_openapi.py --stdout   # 打印到标准输出

**在生产机上跑是安全的**：本脚本只构造 ASGI app 对象并调用 `app.openapi()`，不触发
FastAPI startup 事件——`init_db()` 挂在 `app/bootstrap/lifecycle.py` 的 startup 里，
所以不会用 `.env` 的生产库自动迁移，也不建任何连接。

范围与口径：

* 只导出客户端真正调用的 ``/v1/...``。规范前缀 ``/v1/products/zhaoxi`` 与
  ``/api/v1/products/zhaoxi`` 是同一批路由的另外两个挂载点，导出会产生重复条目，
  故一并排除；公网 ``/api/v1/xxx`` 经 nginx 剥 ``/api`` 后正是这里的 ``/v1/xxx``。
* 输出是**确定性**的（键排序 + 固定缩进），否则 CI 的 snapshot 比对会因字典顺序抖动而红。
* 只有补了 ``response_model`` 的主链路端点才有真实响应 schema；其余端点当前只冻结路径与
  请求体，这是 CONTRACT-001 的既定分步（见 M1 服务端计划 §2.15）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# 客户端契约的挂载前缀，以及需要排除的同源重复挂载点。
CLIENT_PREFIX = "/v1/"
DUPLICATE_MOUNT_PREFIXES = ("/v1/products/", "/api/v1/products/")

SNAPSHOT_PATH = REPO_ROOT / "docs/products/zhaoxi/openapi/app_v1.json"

# snapshot 描述的是**契约**，不是某次构建。标题/版本固定写死，避免 app 元数据改动
# （比如给 Swagger 换个标题）把整个 snapshot 顶掉。
SNAPSHOT_INFO = {
    "title": "朝夕相伴 App 客户端 API",
    "version": "v1",
    "description": (
        "朝夕相伴移动端 App 对接的 /v1 契约。公网入口 https://ai4company.top/api/v1/，"
        "nginx 剥掉 /api 前缀后即本文档的路径。落地口径见 "
        "docs/products/zhaoxi/app_api_handoff.md。"
    ),
}


def _is_client_path(path: str) -> bool:
    """只保留客户端直连的 /v1 路径，剔除同一批路由的其它挂载点。"""
    if not path.startswith(CLIENT_PREFIX):
        return False
    return not path.startswith(DUPLICATE_MOUNT_PREFIXES)


def _collect_schema_refs(node, found: set[str]) -> None:
    """递归收集 ``#/components/schemas/X`` 引用，用于裁剪 components。"""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.rsplit("/", 1)[-1])
        for value in node.values():
            _collect_schema_refs(value, found)
    elif isinstance(node, list):
        for value in node:
            _collect_schema_refs(value, found)


def build_snapshot() -> dict:
    """构造 client-facing OpenAPI 文档；不触发 startup，不碰数据库。"""
    from app.bootstrap.application import create_app

    spec = create_app().openapi()
    paths = {
        path: operations
        for path, operations in spec.get("paths", {}).items()
        if _is_client_path(path)
    }

    # 只保留被保留路径实际用到的 schema（含它们的传递依赖），避免 admin/bridge 的模型
    # 混进客户端契约，让 snapshot 因无关改动而抖动。
    all_schemas = (spec.get("components") or {}).get("schemas") or {}
    needed: set[str] = set()
    _collect_schema_refs(paths, needed)
    while True:
        pending = set()
        for name in needed:
            _collect_schema_refs(all_schemas.get(name, {}), pending)
        fresh = pending - needed
        if not fresh:
            break
        needed |= fresh

    snapshot = {
        "openapi": spec.get("openapi"),
        "info": dict(SNAPSHOT_INFO),
        "paths": paths,
    }
    if needed:
        snapshot["components"] = {
            "schemas": {name: all_schemas[name] for name in sorted(needed) if name in all_schemas}
        }
    return snapshot


def render(snapshot: dict) -> str:
    """确定性序列化：键排序 + 2 空格缩进 + 结尾换行，保证可 diff、可比对。"""
    return json.dumps(snapshot, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="只比对已提交的 snapshot 是否最新，不写文件；不一致时退出码 1",
    )
    parser.add_argument("--stdout", action="store_true", help="打印到标准输出，不写文件")
    parser.add_argument(
        "--output",
        type=Path,
        default=SNAPSHOT_PATH,
        help=f"输出路径（默认 {SNAPSHOT_PATH.relative_to(REPO_ROOT)}）",
    )
    args = parser.parse_args()

    rendered = render(build_snapshot())
    if args.stdout:
        sys.stdout.write(rendered)
        return 0
    if args.check:
        if not args.output.exists():
            print(f"snapshot 不存在：{args.output}", file=sys.stderr)
            return 1
        if args.output.read_text(encoding="utf-8") != rendered:
            print(
                f"snapshot 已过期：{args.output}\n"
                "请运行 .venv/bin/python scripts/export_openapi.py 重新导出并提交。",
                file=sys.stderr,
            )
            return 1
        print(f"snapshot 最新：{args.output}")
        return 0

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered, encoding="utf-8")
    print(f"已写入 {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
