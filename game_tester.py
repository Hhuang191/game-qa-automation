"""Web Game QA Runner: interaction, performance, evidence and defect reporting."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    category: str = "functional"


@dataclass
class ActionLog:
    timestamp: str
    action: str
    passed: bool
    detail: str


@dataclass
class Result:
    game: str
    target_id: str
    url: str
    viewport: str
    started_at: str
    duration_ms: int = 0
    checks: list[Check] = field(default_factory=list)
    actions: list[ActionLog] = field(default_factory=list)
    console_errors: list[str] = field(default_factory=list)
    failed_requests: list[str] = field(default_factory=list)
    screenshot: str = ""
    failure_screenshot: str = ""
    video: str = ""
    metrics: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)


def add(result: Result, name: str, passed: bool, detail: str, category: str = "functional") -> None:
    result.checks.append(Check(name, bool(passed), str(detail), category))


def stamp() -> str:
    return datetime.now().isoformat(timespec="milliseconds")


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return os.path.expandvars(value)
    if isinstance(value, list):
        return [expand_env(x) for x in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


def run_action(page: Page, action: dict[str, Any]) -> None:
    kind = action["type"]
    if kind == "click_text":
        page.get_by_text(action["text"], exact=False).first.click(timeout=action.get("timeout", 5000))
    elif kind == "click_selector":
        page.locator(action["selector"]).first.click(timeout=action.get("timeout", 5000))
    elif kind == "key_hold":
        page.keyboard.down(action["key"])
        page.wait_for_timeout(action.get("ms", 400))
        page.keyboard.up(action["key"])
    elif kind == "press":
        page.keyboard.press(action["key"])
    elif kind == "canvas_fire":
        canvas = page.locator("canvas").first
        box = canvas.bounding_box()
        if not box:
            raise RuntimeError("canvas is not visible")
        x = box["x"] + box["width"] * action.get("x", 0.8)
        y = box["y"] + box["height"] * action.get("y", 0.45)
        page.mouse.move(x, y)
        page.mouse.down()
        page.wait_for_timeout(action.get("ms", 600))
        page.mouse.up()
    elif kind == "wait":
        page.wait_for_timeout(action.get("ms", 500))
    elif kind == "assert_text":
        page.get_by_text(action["text"], exact=False).first.wait_for(state="visible", timeout=action.get("timeout", 5000))
    else:
        raise ValueError(f"Unknown action: {kind}")


def sample_fps(page: Page, duration_ms: int = 1600) -> float:
    return float(page.evaluate("""ms => new Promise(resolve => {
      let frames = 0; const start = performance.now();
      function tick(now) { frames++; if (now - start >= ms) resolve(frames * 1000 / (now - start)); else requestAnimationFrame(tick); }
      requestAnimationFrame(tick);
    })""", duration_ms))


def collect_metrics(page: Page, fps_ms: int) -> dict[str, Any]:
    fps = sample_fps(page, fps_ms)
    raw = page.evaluate("""() => {
      const n = performance.getEntriesByType('navigation')[0];
      const resources = performance.getEntriesByType('resource');
      const memory = performance.memory || {};
      return {
        domContentLoadedMs: n ? Math.round(n.domContentLoadedEventEnd) : 0,
        loadCompleteMs: n ? Math.round(n.loadEventEnd) : 0,
        transferredKB: Math.round(resources.reduce((s, r) => s + (r.transferSize || 0), 0) / 1024),
        resourceCount: resources.length,
        jsHeapUsedMB: memory.usedJSHeapSize ? +(memory.usedJSHeapSize / 1048576).toFixed(1) : null,
        domNodes: document.getElementsByTagName('*').length
      };
    }""")
    raw["averageFps"] = round(fps, 1)
    return raw


def run_case(browser, target: dict[str, Any], viewport: dict[str, Any], out_dir: Path, record_video: bool) -> Result:
    label = viewport["name"]
    result = Result(target["name"], target["id"], target["url"], label, stamp())
    started = time.perf_counter()
    context_args: dict[str, Any] = {"viewport": {"width": viewport["width"], "height": viewport["height"]}}
    video_dir = out_dir / "videos"
    if record_video:
        video_dir.mkdir(parents=True, exist_ok=True)
        context_args.update(record_video_dir=str(video_dir), record_video_size={"width": min(viewport["width"], 1280), "height": min(viewport["height"], 720)})
    context = browser.new_context(**context_args)
    page = context.new_page()
    video = page.video
    page.on("console", lambda msg: result.console_errors.append(msg.text) if msg.type == "error" and "Failed to load resource" not in msg.text else None)
    page.on("pageerror", lambda exc: result.console_errors.append(str(exc)))
    page.on("requestfailed", lambda req: result.failed_requests.append(f"{req.method} {req.url}"))
    page.on("response", lambda res: result.failed_requests.append(f"HTTP {res.status} {res.url}") if res.status >= 400 and "favicon" not in res.url else None)
    try:
        response = page.goto(target["url"], wait_until="domcontentloaded", timeout=target.get("navigation_timeout_ms", 30000))
        page.wait_for_timeout(target.get("settle_ms", 900))
        status = response.status if response else 0
        add(result, "页面可访问", 200 <= status < 400, f"HTTP {status}", "smoke")
        title = page.title()
        add(result, "页面标题", bool(title.strip()), title or "标题为空", "smoke")
        canvas_count = page.locator("canvas").count()
        expect_canvas = target.get("expect_canvas", True)
        add(result, "渲染容器", canvas_count > 0 if expect_canvas else page.locator(target.get("render_selector", "main")).count() > 0,
            f"canvas={canvas_count}, mode={'canvas' if expect_canvas else 'dom'}", "render")
        button_count = page.locator("button").count()
        add(result, "交互控件", button_count > 0, f"检测到 {button_count} 个按钮", "smoke")
        overflow = int(page.evaluate("document.documentElement.scrollWidth - window.innerWidth"))
        add(result, "横向溢出", overflow <= target.get("max_overflow_px", 8), f"超出视口 {overflow}px", "compatibility")

        render = page.locator("canvas").first if canvas_count else page.locator(target.get("render_selector", "main")).first
        before = render.screenshot() if render.count() else b""
        action_errors: list[str] = []
        for action in target.get("actions", []):
            label_action = json.dumps(action, ensure_ascii=False)
            try:
                run_action(page, action)
                result.actions.append(ActionLog(stamp(), label_action, True, "完成"))
            except Exception as exc:
                result.actions.append(ActionLog(stamp(), label_action, bool(action.get("optional")), repr(exc)))
                if not action.get("optional"):
                    action_errors.append(f"{action['type']}: {exc}")
        add(result, "脚本化操作", not action_errors, "全部完成" if not action_errors else "; ".join(action_errors), "interaction")
        page.wait_for_timeout(650)
        if before and target.get("expect_visual_change", True):
            after = render.screenshot()
            changed = hashlib.sha256(before).digest() != hashlib.sha256(after).digest()
            add(result, "画面变化", changed, "操作前后画面已变化" if changed else "操作前后画面无明显变化", "render")

        result.metrics = collect_metrics(page, target.get("fps_sample_ms", 1600))
        perf = target.get("performance", {})
        fps_min = perf.get("fps_min", 30)
        memory_max = perf.get("memory_mb_max", 300)
        load_max = perf.get("dom_content_loaded_ms_max", 5000)
        add(result, "FPS", result.metrics["averageFps"] >= fps_min, f"平均 {result.metrics['averageFps']} FPS，阈值 >= {fps_min}", "performance")
        memory = result.metrics.get("jsHeapUsedMB")
        add(result, "JavaScript内存", memory is None or memory <= memory_max, f"{memory if memory is not None else '浏览器未提供'} MB，阈值 <= {memory_max}", "performance")
        load = result.metrics.get("domContentLoadedMs", 0)
        add(result, "加载耗时", 0 < load <= load_max, f"DOMContentLoaded {load} ms，阈值 <= {load_max}", "performance")
        add(result, "脚本错误", len(result.console_errors) == 0, f"{len(result.console_errors)} 条console/page错误", "stability")
        add(result, "网络失败", len(result.failed_requests) == 0, f"{len(result.failed_requests)} 个失败请求", "stability")
        shot = out_dir / f"{target['id']}-{label}.png"
        page.screenshot(path=str(shot), full_page=True)
        result.screenshot = shot.name
        if not result.passed:
            fail = out_dir / f"FAIL-{target['id']}-{label}.png"
            page.screenshot(path=str(fail), full_page=True)
            result.failure_screenshot = fail.name
    except Exception as exc:
        add(result, "测试执行", False, repr(exc), "blocker")
        try:
            fail = out_dir / f"FAIL-{target['id']}-{label}.png"
            page.screenshot(path=str(fail), full_page=True)
            result.failure_screenshot = fail.name
        except Exception:
            pass
    finally:
        result.duration_ms = round((time.perf_counter() - started) * 1000)
        context.close()
        if record_video and video:
            try:
                video_name = f"{target['id']}-{label}.webm"
                video.save_as(str(video_dir / video_name))
                result.video = f"videos/{video_name}"
            except Exception as exc:
                result.actions.append(ActionLog(stamp(), "保存录像", False, repr(exc)))
    return result


def defect_severity(check: Check) -> str:
    if check.category in {"blocker", "smoke", "stability"}: return "High"
    if check.category in {"render", "interaction", "performance"}: return "Medium"
    return "Low"


def build_defects(results: list[Result]) -> list[dict[str, Any]]:
    defects = []
    number = 1
    for r in results:
        for c in r.checks:
            if c.passed:
                continue
            defects.append({
                "id": f"AUTO-{number:03d}", "game": r.game, "viewport": r.viewport,
                "title": f"[{r.viewport}] {c.name}检查未通过", "severity": defect_severity(c),
                "actual": c.detail, "expected": f"{c.name}满足配置阈值或预期行为",
                "evidence": r.failure_screenshot or r.screenshot, "video": r.video,
                "console_errors": r.console_errors[:5], "failed_requests": r.failed_requests[:5],
                "action_log": [asdict(x) for x in r.actions]
            })
            number += 1
    return defects


def write_logs(results: list[Result], out_dir: Path) -> None:
    lines = []
    for r in results:
        lines.append(f"[{r.started_at}] START {r.game} / {r.viewport} / {r.url}")
        lines.extend(f"[{a.timestamp}] {'PASS' if a.passed else 'FAIL'} {a.action} | {a.detail}" for a in r.actions)
        lines.extend(f"[CONSOLE] {x}" for x in r.console_errors)
        lines.extend(f"[NETWORK] {x}" for x in r.failed_requests)
        lines.append(f"[{stamp()}] END passed={r.passed} duration_ms={r.duration_ms} metrics={json.dumps(r.metrics, ensure_ascii=False)}")
    (out_dir / "operation.log").write_text("\n".join(lines), encoding="utf-8")


def write_defects(defects: list[dict[str, Any]], out_dir: Path) -> None:
    (out_dir / "defects.json").write_text(json.dumps(defects, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 自动缺陷报告", ""]
    if not defects:
        lines += ["本轮测试未自动发现阻断、功能、性能或稳定性异常。"]
    for d in defects:
        lines += [f"## {d['id']} {d['title']}", f"- 严重程度：{d['severity']}", f"- 游戏/视口：{d['game']} / {d['viewport']}", f"- 实际结果：{d['actual']}", f"- 预期结果：{d['expected']}", f"- 证据：{d['evidence'] or '无'}", ""]
    (out_dir / "defects.md").write_text("\n".join(lines), encoding="utf-8")


def write_conclusion(results: list[Result], defects: list[dict[str, Any]], out_dir: Path) -> str:
    checks = [c for r in results for c in r.checks]
    passed = sum(c.passed for c in checks)
    perf = [r.metrics for r in results if r.metrics]
    avg_fps = round(sum(x.get("averageFps", 0) for x in perf) / len(perf), 1) if perf else 0
    avg_mem = round(sum(x.get("jsHeapUsedMB") or 0 for x in perf) / len(perf), 1) if perf else 0
    decision = "PASS - 可进入下一阶段验证" if not defects else "CONDITIONAL - 修复高/中风险问题后回归"
    text = f"""# 自动化测试结论

- 测试对象：{len(results)} 个游戏/视口组合
- 检查项：{passed}/{len(checks)} 通过
- 自动缺陷：{len(defects)} 个
- 平均FPS：{avg_fps}
- 平均JavaScript内存：{avg_mem} MB
- 质量结论：{decision}

说明：自动化结果用于冒烟与回归，不替代玩法体验、数值平衡、长时间稳定性和真机兼容性测试。
"""
    (out_dir / "conclusion.md").write_text(text, encoding="utf-8")
    return decision


def write_html(results: list[Result], defects: list[dict[str, Any]], decision: str, out_dir: Path) -> Path:
    total = sum(len(x.checks) for x in results); passed = sum(c.passed for x in results for c in x.checks)
    cards = []
    for r in results:
        rows = "".join(f"<tr><td class='{'' if c.passed else 'bad'}'>{'PASS' if c.passed else 'FAIL'}</td><td>{html.escape(c.name)}</td><td>{html.escape(c.detail)}</td></tr>" for c in r.checks)
        action_rows = "".join(f"<li class='{'' if a.passed else 'bad'}'>{html.escape(a.action)} - {html.escape(a.detail)}</li>" for a in r.actions)
        metrics = "".join(f"<b>{html.escape(k)}<span>{html.escape(str(v))}</span></b>" for k, v in r.metrics.items())
        media = f"<img src='{r.screenshot}' alt='测试截图'>" if r.screenshot else ""
        if r.video: media += f"<a class='video' href='{r.video}'>查看操作录像</a>"
        cards.append(f"""<section><div class='title'><div><small>{html.escape(r.viewport)}</small><h2>{html.escape(r.game)}</h2><a href='{html.escape(r.url)}'>{html.escape(r.url)}</a></div><b class='score {'ok' if r.passed else 'fail'}'>{'通过' if r.passed else '需检查'}</b></div><div class='metrics'>{metrics}</div><div class='grid'><div><table>{rows}</table><h3>操作日志</h3><ul>{action_rows}</ul></div><div>{media}</div></div><footer>耗时 {r.duration_ms} ms</footer></section>""")
    defects_html = "".join(f"<tr><td>{d['id']}</td><td>{html.escape(d['severity'])}</td><td>{html.escape(d['title'])}</td><td>{html.escape(d['actual'])}</td></tr>" for d in defects) or "<tr><td colspan='4'>无自动缺陷</td></tr>"
    report = out_dir / "report.html"
    report.write_text(f"""<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>网页游戏自动化测试报告</title><style>*{{box-sizing:border-box}}body{{margin:0;background:#07110f;color:#dff8f1;font:14px Arial,'Microsoft YaHei';padding:45px}}header,section,.defects{{max-width:1180px;margin:auto auto 25px}}h1{{font-size:42px;margin:8px 0}}small,a{{color:#79a095}}.summary,.metrics{{display:flex;gap:8px;flex-wrap:wrap}}.summary b,.metrics b{{background:#11241f;border:1px solid #31584e;padding:10px 15px}}.metrics{{margin:18px 0}}.metrics b{{font-size:10px;color:#78958d}}.metrics span{{display:block;color:#fff;font-size:16px;margin-top:4px}}section,.defects{{background:#0d1d19;border:1px solid #2a5047;padding:25px}}.title{{display:flex;justify-content:space-between}}.score{{padding:10px 15px;height:fit-content}}.ok{{background:#54e6b8;color:#04100c}}.fail{{background:#ff6673}}.grid{{display:grid;grid-template-columns:1fr 42%;gap:25px}}img{{width:100%;border:1px solid #31584e}}.video{{display:block;padding:10px;text-align:center;background:#19352e}}table{{width:100%;border-collapse:collapse}}td{{padding:9px;border-bottom:1px solid #233d36}}td:first-child{{color:#61edc0;font:bold 11px monospace}}.bad{{color:#ff6b76}}h3{{font-size:12px;margin-top:24px;color:#819f97}}li{{color:#b1c5bf;font-size:12px;margin:6px}}footer{{border-top:1px solid #233d36;margin-top:18px;padding-top:12px;color:#66847c;font:11px monospace}}@media(max-width:800px){{.grid{{grid-template-columns:1fr}}body{{padding:18px}}}}</style><header><small>GAME QA AUTOMATION / {datetime.now():%Y-%m-%d %H:%M}</small><h1>网页游戏自动化测试报告</h1><p>{html.escape(decision)}</p><div class='summary'><b>检查 {total}</b><b>通过 {passed}</b><b>失败 {total-passed}</b><b>缺陷 {len(defects)}</b></div></header>{''.join(cards)}<div class='defects'><h2>自动缺陷清单</h2><table>{defects_html}</table></div></html>""", encoding="utf-8")
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="Run automated game smoke, interaction and performance tests")
    ap.add_argument("--config", default="games.json")
    ap.add_argument("--output", default="reports/latest")
    ap.add_argument("--chrome", default=r"C:\Program Files\Google\Chrome\Application\chrome.exe")
    ap.add_argument("--no-video", action="store_true")
    args = ap.parse_args()
    config = expand_env(json.loads(Path(args.config).read_text(encoding="utf-8")))
    out = Path(args.output); out.mkdir(parents=True, exist_ok=True)
    results: list[Result] = []
    with sync_playwright() as p:
        launch_args = {"headless": True, "args": ["--disable-gpu", "--enable-precise-memory-info"]}
        if args.chrome and Path(args.chrome).exists(): launch_args["executable_path"] = args.chrome
        browser = p.chromium.launch(**launch_args)
        for target in config["targets"]:
            if not target.get("url") or "$" in target.get("url", ""):
                continue
            for viewport in config["viewports"]:
                results.append(run_case(browser, target, viewport, out, not args.no_video))
        browser.close()
    raw = [asdict(x) | {"passed": x.passed} for x in results]
    (out / "results.json").write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    write_logs(results, out)
    defects = build_defects(results); write_defects(defects, out)
    decision = write_conclusion(results, defects, out)
    report = write_html(results, defects, decision, out)
    print(report.resolve())
    return 0 if results and all(x.passed for x in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
