# Game QA Automation Kit

面向网页游戏和安卓游戏的自动化测试作品。当前示例覆盖射击游戏 ROOT//BREACH 与卡牌游戏 NULL DECK，提交代码后可由 GitHub Actions 自动执行。

## 已实现能力

- 射击游戏：启动、移动、持续射击、Canvas画面变化与桌面/移动视口检查
- 卡牌游戏：进入匹配、可用卡牌点击、结束回合、敌我回合状态断言
- 性能监控：网页平均FPS、JavaScript堆内存、DOMContentLoaded、资源数与传输量
- 测试证据：全页截图、失败截图、WebM录像、逐步操作日志、Console错误与网络失败
- 自动报告：JSON、HTML、缺陷清单和测试结论；失败检查自动转为缺陷记录
- 安卓测试：ADB安装/启动/点击/滑动/按键、冷启动、PSS内存、gfxinfo帧数据、logcat崩溃与ANR
- Appium扩展：UiAutomator2元素查找、点击和可见性断言
- 持续集成：GitHub Actions检出两款游戏、启动本地服务、自动回归并上传测试证据

## 网页游戏测试

安装依赖：

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

分别启动两款游戏的本地开发服务，端口为3101和3102，再执行：

```powershell
python game_tester.py --config root.local.json --output reports/root
python game_tester.py --config deck.local.json --output reports/deck
```

在Linux CI中不指定系统Chrome：

```bash
python game_tester.py --config games.local.json --output reports/latest --chrome ""
```

报告目录包含：

- `report.html`：可视化测试报告
- `results.json`：结构化检查、性能指标和证据路径
- `defects.json` / `defects.md`：自动缺陷报告
- `conclusion.md`：自动测试结论
- `operation.log`：操作、Console、网络与性能日志
- `*.png` / `videos/*.webm`：截图与操作录像

## 安卓游戏测试

安装 Android Platform Tools，将 `adb` 加入PATH。需要元素级定位时，再启动Appium 2与UiAutomator2驱动并安装扩展依赖：

```powershell
python -m pip install -r requirements-android.txt
python android_tester.py --config android.example.json --dry-run
python android_tester.py --config android.example.json --output reports/android
```

将 `android.example.json` 中的包名、Activity、坐标动作和Appium元素改成目标游戏信息。无真机时可先使用 `--dry-run` 校验配置；真实FPS、内存、截图、录像和logcat需要已授权设备或模拟器。

## 配置说明

网页配置支持 `click_text`、`click_selector`、`key_hold`、`press`、`canvas_fire`、`wait`、`assert_text`。每个游戏可单独设置 `expect_canvas`、`render_selector`、允许溢出值和性能阈值。

安卓配置支持 `tap`、`swipe`、`keyevent`、`wait`、`assert_text`，并可选配置 `appium_actions`。示例仅提供通用结构，不代表已在特定APK真机上通过。

## GitHub Actions

工作流位于 `.github/workflows/game-qa.yml`。推送或提交Pull Request后会自动：

1. 检出测试工具与两款游戏仓库；
2. 安装Python、Playwright和Node依赖；
3. 顺序启动游戏并运行桌面/移动端回归；
4. 无论成功或失败都上传完整测试证据；
5. 校验安卓测试配置，真机执行可接入自托管Runner。

## 使用边界

浏览器堆内存仅代表Chrome提供的JavaScript堆指标；安卓FPS为`gfxinfo framestats`估算值。自动化适合冒烟与版本回归，不替代玩法体验、数值平衡、弱网、长时间稳定性和多机型人工测试。
