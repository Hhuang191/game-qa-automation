"""Android Game QA Runner using ADB with optional Appium interaction."""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import time
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass
class Check:
    name: str
    passed: bool
    detail: str
    category: str = "functional"


@dataclass
class AndroidResult:
    device: str
    package: str
    started_at: str
    checks: list[Check] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    logcat_errors: list[str] = field(default_factory=list)
    screenshot: str = ""
    video: str = ""

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(item.passed for item in self.checks)


def run(command: list[str], timeout: int = 30, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, text=True, encoding="utf-8", errors="replace", capture_output=True,
                          timeout=timeout, check=check)


def adb(adb_bin: str, device: str, *args: str, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return run([adb_bin, "-s", device, *args], timeout=timeout)


def add(result: AndroidResult, name: str, passed: bool, detail: str, category: str = "functional") -> None:
    result.checks.append(Check(name, bool(passed), str(detail), category))


def connected_devices(adb_bin: str) -> list[str]:
    output = run([adb_bin, "devices"], timeout=15).stdout.splitlines()[1:]
    return [line.split()[0] for line in output if line.strip().endswith("\tdevice")]


def package_installed(adb_bin: str, device: str, package: str) -> bool:
    return package in adb(adb_bin, device, "shell", "pm", "list", "packages", package).stdout


def parse_launch_time(text: str) -> int | None:
    match = re.search(r"(?:TotalTime|WaitTime):\s*(\d+)", text)
    return int(match.group(1)) if match else None


def parse_memory(text: str) -> float | None:
    match = re.search(r"TOTAL\s+(\d+)", text)
    return round(int(match.group(1)) / 1024, 1) if match else None


def parse_fps(text: str) -> float | None:
    frames = []
    in_profile = False
    for line in text.splitlines():
        if line.startswith("---PROFILEDATA---"):
            in_profile = not in_profile
            continue
        if not in_profile or not re.match(r"^\d+,", line):
            continue
        values = line.split(",")
        try:
            intended, completed = int(values[1]), int(values[13])
            if 0 < completed - intended < 1_000_000_000:
                frames.append((completed - intended) / 1_000_000)
        except (ValueError, IndexError):
            pass
    if not frames:
        return None
    average_ms = sum(frames) / len(frames)
    return round(min(60.0, 1000 / average_ms), 1) if average_ms else None


def pull_screenshot(adb_bin: str, device: str, destination: Path) -> None:
    remote = "/sdcard/game_qa_failure.png"
    adb(adb_bin, device, "shell", "screencap", "-p", remote)
    adb(adb_bin, device, "pull", remote, str(destination))
    adb(adb_bin, device, "shell", "rm", remote)


def run_adb_actions(adb_bin: str, device: str, actions: list[dict[str, Any]], result: AndroidResult) -> None:
    for action in actions:
        started = datetime.now().isoformat(timespec="milliseconds")
        ok, detail = True, "完成"
        try:
            kind = action["type"]
            if kind == "tap":
                proc = adb(adb_bin, device, "shell", "input", "tap", str(action["x"]), str(action["y"]))
            elif kind == "swipe":
                proc = adb(adb_bin, device, "shell", "input", "swipe", str(action["x1"]), str(action["y1"]),
                           str(action["x2"]), str(action["y2"]), str(action.get("ms", 350)))
            elif kind == "keyevent":
                proc = adb(adb_bin, device, "shell", "input", "keyevent", str(action["keycode"]))
            elif kind == "wait":
                time.sleep(action.get("ms", 500) / 1000)
                proc = subprocess.CompletedProcess([], 0, "", "")
            elif kind == "assert_text":
                remote = "/sdcard/game_qa_window.xml"
                adb(adb_bin, device, "shell", "uiautomator", "dump", remote)
                dump = adb(adb_bin, device, "shell", "cat", remote).stdout
                proc = subprocess.CompletedProcess([], 0 if action["text"] in dump else 1, dump, "")
                detail = f"文本 {'已找到' if proc.returncode == 0 else '未找到'}：{action['text']}"
            else:
                raise ValueError(f"未知ADB动作：{kind}")
            if proc.returncode:
                raise RuntimeError(proc.stderr or detail)
        except Exception as exc:
            ok, detail = bool(action.get("optional")), repr(exc)
        result.actions.append({"timestamp": started, "action": action, "passed": ok, "detail": detail})


def appium_ready(server_url: str) -> bool:
    try:
        with urllib.request.urlopen(server_url.rstrip("/") + "/status", timeout=3) as response:
            return response.status == 200
    except Exception:
        return False


def run_appium_actions(config: dict[str, Any], device: str, result: AndroidResult) -> None:
    try:
        from appium import webdriver
        from appium.options.android import UiAutomator2Options
        from appium.webdriver.common.appiumby import AppiumBy
    except ImportError as exc:
        add(result, "Appium依赖", False, f"未安装Appium-Python-Client：{exc}", "environment")
        return
    server = config.get("appium_server", "http://127.0.0.1:4723")
    if not appium_ready(server):
        add(result, "Appium服务", False, f"服务不可访问：{server}", "environment")
        return
    options = UiAutomator2Options().load_capabilities({
        "platformName": "Android", "appium:automationName": "UiAutomator2",
        "appium:deviceName": device, "appium:udid": device,
        "appium:appPackage": config["package"], "appium:appActivity": config["activity"],
        "appium:noReset": True,
    })
    driver = webdriver.Remote(server, options=options)
    try:
        for action in config.get("appium_actions", []):
            started = datetime.now().isoformat(timespec="milliseconds")
            ok, detail = True, "完成"
            try:
                by = {"id": AppiumBy.ID, "text": AppiumBy.ANDROID_UIAUTOMATOR,
                      "accessibility_id": AppiumBy.ACCESSIBILITY_ID}[action.get("by", "id")]
                value = action["value"]
                if action.get("by") == "text":
                    value = f'new UiSelector().textContains("{value}")'
                element = driver.find_element(by, value)
                if action["type"] == "click":
                    element.click()
                elif action["type"] == "assert_visible":
                    ok = element.is_displayed()
                else:
                    raise ValueError(f"未知Appium动作：{action['type']}")
            except Exception as exc:
                ok, detail = bool(action.get("optional")), repr(exc)
            result.actions.append({"timestamp": started, "action": action, "passed": ok, "detail": detail})
    finally:
        driver.quit()
    add(result, "Appium操作", all(item["passed"] for item in result.actions), "Appium动作执行完成", "interaction")


def write_report(result: AndroidResult, output: Path) -> None:
    payload = asdict(result) | {"passed": result.passed}
    (output / "android-results.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    failed = [item for item in result.checks if not item.passed]
    defects = [{"id": f"ANDROID-{i:03d}", "title": item.name, "category": item.category,
                "actual": item.detail, "evidence": result.screenshot, "video": result.video}
               for i, item in enumerate(failed, 1)]
    (output / "android-defects.json").write_text(json.dumps(defects, ensure_ascii=False, indent=2), encoding="utf-8")
    verdict = "PASS - 可继续兼容性回归" if result.passed else "CONDITIONAL - 修复失败项后回归"
    rows = "\n".join(f"- {'PASS' if item.passed else 'FAIL'} {item.name}：{item.detail}" for item in result.checks)
    actions = "\n".join(f"- {'PASS' if item['passed'] else 'FAIL'} {json.dumps(item['action'], ensure_ascii=False)}"
                        for item in result.actions) or "- 未配置操作"
    (output / "android-conclusion.md").write_text(
        f"# Android游戏自动化测试结论\n\n- 设备：{result.device}\n- 包名：{result.package}\n- 结论：{verdict}\n\n"
        f"## 检查结果\n\n{rows}\n\n## 操作日志\n\n{actions}\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run Android game tests with ADB and optional Appium")
    parser.add_argument("--config", default="android.example.json")
    parser.add_argument("--output", default="reports/android")
    parser.add_argument("--device", default="")
    parser.add_argument("--adb", default="adb")
    parser.add_argument("--dry-run", action="store_true", help="Only validate configuration and dependencies")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    output = Path(args.output); output.mkdir(parents=True, exist_ok=True)
    if args.dry_run:
        required = ["package", "activity", "actions"]
        missing = [key for key in required if key not in config]
        print(json.dumps({"config_valid": not missing, "missing": missing, "adb": shutil.which(args.adb)}, ensure_ascii=False))
        return 1 if missing else 0
    if not shutil.which(args.adb):
        print("未找到adb，请安装Android Platform Tools并加入PATH。")
        return 2
    devices = connected_devices(args.adb)
    device = args.device or (devices[0] if devices else "")
    if not device:
        print("未检测到已授权的Android设备或模拟器。")
        return 2
    result = AndroidResult(device, config["package"], datetime.now().isoformat(timespec="seconds"))
    add(result, "设备连接", device in connected_devices(args.adb), device, "environment")
    if config.get("apk"):
        install = adb(args.adb, device, "install", "-r", config["apk"], timeout=180)
        add(result, "APK安装", install.returncode == 0, install.stdout[-300:] or install.stderr[-300:], "install")
    add(result, "应用存在", package_installed(args.adb, device, config["package"]), config["package"], "install")
    adb(args.adb, device, "logcat", "-c")
    adb(args.adb, device, "shell", "dumpsys", "gfxinfo", config["package"], "reset")
    launch = adb(args.adb, device, "shell", "am", "start", "-W", "-n", f"{config['package']}/{config['activity']}")
    launch_ms = parse_launch_time(launch.stdout)
    result.metrics["coldLaunchMs"] = launch_ms
    launch_limit = config.get("performance", {}).get("cold_launch_ms_max", 5000)
    add(result, "冷启动", launch.returncode == 0 and launch_ms is not None and launch_ms <= launch_limit,
        f"{launch_ms} ms，阈值 <= {launch_limit}" if launch_ms is not None else launch.stdout[-300:], "performance")
    remote_video = "/sdcard/game_qa_run.mp4"
    recorder = subprocess.Popen([args.adb, "-s", device, "shell", "screenrecord", "--time-limit",
                                 str(config.get("video_seconds", 30)), remote_video], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    time.sleep(config.get("settle_seconds", 2))
    run_adb_actions(args.adb, device, config.get("actions", []), result)
    if config.get("appium_actions"):
        run_appium_actions(config, device, result)
    adb(args.adb, device, "shell", "pkill", "-INT", "screenrecord")
    try:
        recorder.wait(timeout=5)
    except subprocess.TimeoutExpired:
        recorder.terminate()
    video = output / "android-run.mp4"
    pulled = adb(args.adb, device, "pull", remote_video, str(video))
    if pulled.returncode == 0 and video.exists(): result.video = video.name
    adb(args.adb, device, "shell", "rm", remote_video)
    screen = output / "android-final.png"; pull_screenshot(args.adb, device, screen); result.screenshot = screen.name
    memory = parse_memory(adb(args.adb, device, "shell", "dumpsys", "meminfo", config["package"]).stdout)
    fps = parse_fps(adb(args.adb, device, "shell", "dumpsys", "gfxinfo", config["package"], "framestats").stdout)
    result.metrics.update({"memoryPssMB": memory, "estimatedFps": fps})
    thresholds = config.get("performance", {})
    add(result, "内存", memory is not None and memory <= thresholds.get("memory_mb_max", 500),
        f"PSS {memory} MB，阈值 <= {thresholds.get('memory_mb_max', 500)}", "performance")
    add(result, "FPS", fps is None or fps >= thresholds.get("fps_min", 30),
        f"估算 {fps if fps is not None else '无帧数据'} FPS，阈值 >= {thresholds.get('fps_min', 30)}", "performance")
    logs = adb(args.adb, device, "logcat", "-d", "-v", "time", timeout=60).stdout
    result.logcat_errors = [line for line in logs.splitlines()
                            if config["package"] in line and re.search(r"FATAL EXCEPTION|ANR in|\sE\s", line)][-100:]
    (output / "logcat.txt").write_text(logs, encoding="utf-8")
    add(result, "崩溃与ANR", not result.logcat_errors, f"发现 {len(result.logcat_errors)} 条高风险日志", "stability")
    add(result, "自动化操作", all(item["passed"] for item in result.actions),
        f"{sum(item['passed'] for item in result.actions)}/{len(result.actions)} 通过", "interaction")
    write_report(result, output)
    print((output / "android-conclusion.md").resolve())
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
