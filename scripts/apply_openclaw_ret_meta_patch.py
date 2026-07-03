#!/usr/bin/env python
"""openclaw 核心 + openclaw-weixin 插件「sendMessage 业务码打通到 ai4all」手术补丁——
安装态 dist 幂等打补丁器（可在 aliyun2 / 新机一致重放）。

背景：让 iLink `sendMessage` 的 `ret`/`errcode`/`errmsg` 从"被两层丢弃"打通到 ai4all，
终结沉默用户主动消息的"假成功"。源码级改动见 patches/openclaw-*.src.patch；
完整设计与验证见 docs/troubleshooting/weixin_context_token_send_semantics.md 第五节。

为何不 rebuild 覆盖：运行态与源码严重漂移（核心 npm 安装版、插件安装版领先源码仓库、
且安装态独有 abort-signal/runId/logout 等），整文件覆盖会大面积回退。故只逐处手术拼接。

特性：
- 幂等：已打过（marker 命中）则跳过。
- 安全：锚点缺失或不唯一 → 该文件不动、报错，绝不猜测/半改。
- 自动定位：核心 chunk 名随版本变（按 `buildGatewayDeliveryPayload` 搜）；插件 project
  目录含随机 hash（按路径 glob）。可用环境变量 OPENCLAW_CORE_DIST / OPENCLAW_WEIXIN_DIST 覆盖。
- 每个改动文件先备份（`.bak-retmeta`，不覆盖已有备份）再改，改完 `node --check`。

用法：
    python scripts/apply_openclaw_ret_meta_patch.py            # dry-run，只报告将做什么
    python scripts/apply_openclaw_ret_meta_patch.py --apply    # 真正写入
    # 打完需人工重启网关：systemctl --user restart openclaw-gateway.service
"""
from __future__ import annotations

import argparse
import glob
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

BACKUP_SUFFIX = ".bak-retmeta"

# ── 定位安装态 dist ────────────────────────────────────────────────────────────

def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def _running_gateway_core_dist() -> Optional[str]:
    """从运行中网关进程 cmdline 取实际加载的核心 dist 目录（最准，绑定真正在跑的 core）。"""
    for cmd in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            parts = Path(cmd).read_bytes().split(b"\x00")
        except Exception:
            continue
        for p in parts:
            s = p.decode("utf-8", "ignore")
            if s.endswith("/openclaw/dist/index.js") and os.path.exists(s):
                return os.path.dirname(s)
    return None


def _find_chunk_in_dir(dist_dir: str) -> List[Path]:
    hits: List[Path] = []
    for js in glob.glob(os.path.join(dist_dir, "*.js")):
        try:
            if "function buildGatewayDeliveryPayload" in Path(js).read_text("utf-8"):
                hits.append(Path(js))
        except Exception:
            continue
    return hits


def locate_core_chunk() -> Path:
    """返回含 buildGatewayDeliveryPayload 的核心 dist chunk（chunk 名随版本变）。

    优先用运行中网关实际加载的 dist；否则 glob tools 安装目录。按 realpath 去重
    （node -> node-vX 软链），排除插件 node_modules 里嵌套的 openclaw 副本（网关不用）。
    """
    override = os.environ.get("OPENCLAW_CORE_DIST", "").strip()
    dirs: List[str] = []
    if override:
        dirs.append(override)
    else:
        # 运行中网关进程实际加载的 dist 是唯一权威（同机可能并存多份 openclaw 安装：
        # aliyun1 tools 安装器在跑、npm-g 那份没跑）。找到即只用它。
        running = _running_gateway_core_dist()
        if running:
            dirs.append(running)
        else:
            # 网关未运行时的兜底：官方安装器 / npm-g 两种布局
            dirs += glob.glob(str(_home() / ".openclaw/tools/*/lib/node_modules/openclaw/dist"))
            dirs += glob.glob(str(_home() / ".npm-global/lib/node_modules/openclaw/dist"))

    hits: List[Path] = []
    for d in dirs:
        hits += _find_chunk_in_dir(d)
    # 排除嵌套插件副本；按 realpath 去重（软链归一）
    seen: dict[str, Path] = {}
    for h in hits:
        sp = str(h)
        if "@tencent-weixin" in sp or "/openclaw-weixin/node_modules/" in sp:
            continue
        seen.setdefault(os.path.realpath(sp), h)
    uniq = sorted(seen.values(), key=lambda p: str(p))
    if not uniq:
        raise FileNotFoundError("找不到核心 send chunk（buildGatewayDeliveryPayload）；可设 OPENCLAW_CORE_DIST")
    if len(uniq) > 1:
        raise RuntimeError(f"核心 send chunk 命中多个（去重后），需人工确认：{uniq}")
    return uniq[0]


def locate_weixin_dist() -> Path:
    override = os.environ.get("OPENCLAW_WEIXIN_DIST", "").strip()
    if override:
        p = Path(override)
        if not p.exists():
            raise FileNotFoundError(f"OPENCLAW_WEIXIN_DIST 不存在：{p}")
        return p
    hits = sorted(set(glob.glob(
        str(_home() / ".openclaw/npm/projects/*/node_modules/@tencent-weixin/openclaw-weixin/dist")
    )))
    if not hits:
        raise FileNotFoundError("找不到 openclaw-weixin 安装态 dist；可设 OPENCLAW_WEIXIN_DIST")
    if len(hits) > 1:
        raise RuntimeError(f"openclaw-weixin dist 命中多个，需人工确认：{hits}")
    return Path(hits[0])


# ── 补丁内容（安装态·编译后 JS 的原始锚点）─────────────────────────────────────

API_OLD = '''/** Send a single message downstream. */
export async function sendMessage(params) {
    await apiPostFetch({
        baseUrl: params.baseUrl,
        endpoint: "ilink/bot/sendmessage",
        body: JSON.stringify({ ...params.body, base_info: buildBaseInfo() }),
        token: params.token,
        timeoutMs: params.timeoutMs ?? DEFAULT_API_TIMEOUT_MS,
        label: "sendMessage",
    });
}'''

API_NEW = '''/** Best-effort parse of the sendmessage response body; never throws (unparseable -> {}). */
function parseSendMessageResp(rawText) {
    const trimmed = (rawText ?? "").trim();
    if (!trimmed)
        return {};
    try {
        const parsed = JSON.parse(trimmed);
        return parsed && typeof parsed === "object" ? parsed : {};
    }
    catch {
        return {};
    }
}
/** Classify a sendmessage outcome from its ret/errcode for logging (ret:-2 semantics TENTATIVE). */
function classifySendMessageResp(resp) {
    const ret = resp.ret;
    if (resp.errcode === SESSION_EXPIRED_ERRCODE)
        return `session_expired(errcode=${SESSION_EXPIRED_ERRCODE})`;
    if (ret === undefined || ret === 0)
        return "accepted";
    if (ret === -2)
        return "ret_-2(TENTATIVE:rate_limit_or_reject)";
    return `error(ret=${ret})`;
}
/** Send a single message downstream. Parses the iLink response body, logs the outcome, returns it. */
export async function sendMessage(params) {
    const rawText = await apiPostFetch({
        baseUrl: params.baseUrl,
        endpoint: "ilink/bot/sendmessage",
        body: JSON.stringify({ ...params.body, base_info: buildBaseInfo() }),
        token: params.token,
        timeoutMs: params.timeoutMs ?? DEFAULT_API_TIMEOUT_MS,
        label: "sendMessage",
    });
    const resp = parseSendMessageResp(rawText);
    const to = params.body?.msg?.to_user_id ?? "";
    logger.info(`sendMessage outcome to=${to} classification=${classifySendMessageResp(resp)} ` +
        `ret=${resp.ret ?? "none"} errcode=${resp.errcode ?? "none"} errmsg=${resp.errmsg ?? "none"} ` +
        `raw=${redactBody(rawText, 500)}`);
    return resp;
}'''

SEND_OLD = '''/**
 * Send a plain text message downstream.
 */
export async function sendMessageWeixin(params) {
    const { to, text, opts } = params;
    if (!opts.contextToken) {
        logger.warn(`sendMessageWeixin: contextToken missing for to=${to}, sending without context`);
    }
    const clientId = generateClientId();
    const req = buildSendMessageReq({
        to,
        contextToken: opts.contextToken,
        runId: opts.runId,
        payload: { text },
        clientId,
    });
    try {
        await sendMessageApi({
            baseUrl: opts.baseUrl,
            token: opts.token,
            timeoutMs: opts.timeoutMs,
            body: req,
        });
    }
    catch (err) {
        logger.error(`sendMessageWeixin: failed to=${to} clientId=${clientId} err=${String(err)}`);
        throw err;
    }
    return { messageId: clientId };
}'''

SEND_NEW = '''/** Lift iLink business fields (ret/errcode/errmsg) into a meta bag; undefined when none present. */
function buildSendResultMeta(resp) {
    if (!resp)
        return undefined;
    const meta = {};
    if (resp.ret !== undefined)
        meta.ret = resp.ret;
    if (resp.errcode !== undefined)
        meta.errcode = resp.errcode;
    if (resp.errmsg !== undefined)
        meta.errmsg = resp.errmsg;
    return Object.keys(meta).length ? meta : undefined;
}
/**
 * Send a plain text message downstream.
 */
export async function sendMessageWeixin(params) {
    const { to, text, opts } = params;
    if (!opts.contextToken) {
        logger.warn(`sendMessageWeixin: contextToken missing for to=${to}, sending without context`);
    }
    const clientId = generateClientId();
    const req = buildSendMessageReq({
        to,
        contextToken: opts.contextToken,
        runId: opts.runId,
        payload: { text },
        clientId,
    });
    let resp;
    try {
        resp = await sendMessageApi({
            baseUrl: opts.baseUrl,
            token: opts.token,
            timeoutMs: opts.timeoutMs,
            body: req,
        });
    }
    catch (err) {
        logger.error(`sendMessageWeixin: failed to=${to} clientId=${clientId} err=${String(err)}`);
        throw err;
    }
    const meta = buildSendResultMeta(resp);
    return meta ? { messageId: clientId, meta } : { messageId: clientId };
}'''

CHAN_OLD = '''        emitWeixinMessageSent({ to: params.to, content: filteredText, success: true, accountId: account.accountId });
        return { channel: "openclaw-weixin", messageId: result.messageId };'''

CHAN_NEW = '''        emitWeixinMessageSent({ to: params.to, content: filteredText, success: true, accountId: account.accountId });
        return result.meta
            ? { channel: "openclaw-weixin", messageId: result.messageId, meta: result.meta }
            : { channel: "openclaw-weixin", messageId: result.messageId };'''


def _replace_edit(text: str, old: str, new: str, marker: str) -> tuple[str, str]:
    """块替换。返回 (新文本, 状态)。"""
    if marker in text:
        return text, "already-applied"
    cnt = text.count(old)
    if cnt == 0:
        return text, "ANCHOR-MISSING"
    if cnt > 1:
        return text, "ANCHOR-AMBIGUOUS"
    return text.replace(old, new, 1), "applied"


def _insert_after_line_edit(text: str, match_substr: str, insert: str, marker: str,
                            keep_indent: bool) -> tuple[str, str]:
    """在唯一含 match_substr 的行后插入一行（可继承该行缩进）。"""
    if marker in text:
        return text, "already-applied"
    lines = text.splitlines(keepends=True)
    idxs = [i for i, ln in enumerate(lines) if match_substr in ln]
    if len(idxs) == 0:
        return text, "ANCHOR-MISSING"
    if len(idxs) > 1:
        return text, "ANCHOR-AMBIGUOUS"
    i = idxs[0]
    line = lines[i]
    indent = ""
    if keep_indent:
        indent = line[: len(line) - len(line.lstrip())]
    nl = "\n" if not line.endswith("\n") else ""
    lines.insert(i + 1, f"{nl}{indent}{insert}\n")
    return "".join(lines), "applied"


def _node_check(path: Path) -> bool:
    try:
        r = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True)
        if r.returncode != 0:
            print(f"    [node --check FAILED] {path}\n{r.stderr.strip()}")
            return False
        return True
    except FileNotFoundError:
        print("    [warn] 未找到 node，跳过 --check")
        return True


def main() -> int:
    ap = argparse.ArgumentParser(description="openclaw ret/meta 手术补丁器（安装态 dist）")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认 dry-run）")
    args = ap.parse_args()

    try:
        core = locate_core_chunk()
        wdist = locate_weixin_dist()
    except Exception as err:  # noqa: BLE001
        print(f"[ERROR] 定位失败：{err}", file=sys.stderr)
        return 2

    api = wdist / "src/api/api.js"
    send = wdist / "src/messaging/send.js"
    chan = wdist / "src/channel.js"
    for p in (api, send, chan):
        if not p.exists():
            print(f"[ERROR] 缺文件：{p}", file=sys.stderr)
            return 2

    print(f"core chunk : {core}")
    print(f"weixin dist: {wdist}\n")

    # (文件, [edit,...])；edit = (kind, ...)
    plan = [
        (core, [("insert", 'if ("pollId" in params.result)',
                 'if ("meta" in params.result) payload.meta = params.result.meta;',
                 "payload.meta = params.result.meta", True)]),
        (api, [
            ("insert", 'import { redactBody, redactUrl } from "../util/redact.js";',
             'import { SESSION_EXPIRED_ERRCODE } from "./session-guard.js";',
             "SESSION_EXPIRED_ERRCODE", False),
            ("replace", API_OLD, API_NEW, "parseSendMessageResp"),
        ]),
        (send, [("replace", SEND_OLD, SEND_NEW, "buildSendResultMeta")]),
        (chan, [("replace", CHAN_OLD, CHAN_NEW, "messageId: result.messageId, meta: result.meta")]),
    ]

    any_error = False
    changed_files: List[Path] = []
    for path, edits in plan:
        text = path.read_text("utf-8")
        new_text = text
        statuses = []
        for edit in edits:
            if edit[0] == "replace":
                _, old, new, marker = edit
                new_text, st = _replace_edit(new_text, old, new, marker)
            else:  # insert
                _, match_substr, insert, marker, keep_indent = edit
                new_text, st = _insert_after_line_edit(new_text, match_substr, insert, marker, keep_indent)
            statuses.append(st)
            if st in ("ANCHOR-MISSING", "ANCHOR-AMBIGUOUS"):
                any_error = True
        print(f"{path.name}: {statuses}")
        if new_text != text:
            changed_files.append(path)
            if args.apply:
                bak = path.with_name(path.name + BACKUP_SUFFIX)
                if not bak.exists():
                    bak.write_text(text, "utf-8")
                    print(f"    backup -> {bak.name}")
                path.write_text(new_text, "utf-8")
                if not _node_check(path):
                    any_error = True

    print()
    if any_error:
        print("[RESULT] 有锚点缺失/歧义或语法校验失败——请人工核对（该文件已保持不改或需回滚备份）。")
        return 1
    if not args.apply:
        print(f"[DRY-RUN] 将改动 {len(changed_files)} 个文件；加 --apply 真正写入。")
    else:
        print(f"[DONE] 已应用；改动 {len(changed_files)} 个文件。")
        print("下一步（人工）：systemctl --user restart openclaw-gateway.service")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
