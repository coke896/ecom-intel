@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   电商竞品情报分析平台
echo   启动完成后在浏览器打开 http://localhost:8001
echo ============================================
echo.
echo 这个项目由【两个进程】组成，本脚本会各开一个窗口：
echo   1) Web 服务   —— 上面这个地址，看图表 / 触发分析
echo   2) 采集 worker —— 认领采集任务、每 5 分钟检查一次告警
echo.
echo ★ 只起 Web 服务的话，采集任务会永远停在 pending。
echo   这不是 bug：Playwright 不能在 API 进程里跑（原因见 app/api/tasks.py 顶部）。
echo.
echo 启动完成后：
echo   · 页面上点"采集" → 入队任务 → 这个 worker 会认领并执行。
echo     mock 源离线可用；smzdm 源默认拒绝联网，会明确告诉你
echo     "未联网（不是被拦截）"以及怎么开——那不是报错，是刻意的开关。
echo   · 想灌入真实数据（重放本地已抓好的真实页面，不联网、不消耗额度）：
echo       另开一个窗口跑  .venv\Scripts\python.exe -m scraper.worker --replay
echo     ★ 它是"灌一次就退出"，不是常驻——常驻的是本脚本起的这个 worker。
echo   · 想真的联网采集就给它加 --allow-network，但请先读 README 的合规那一节。
echo.
echo 关闭哪个窗口就停止哪个进程，两个都关才彻底停。
echo.

rem 先查 venv。不查的话，报错会是"系统找不到指定的路径"，
rem 而真正的原因是虚拟环境还没建——那句提示帮不上任何忙。
if not exist ".venv\Scripts\python.exe" (
    echo [错误] 没找到 .venv\Scripts\python.exe，请先在本目录执行：
    echo.
    echo     python -m venv .venv
    echo     .venv\Scripts\python.exe -m pip install -r requirements.txt
    echo.
    pause
    exit /b 1
)

rem 库文件不存在就先灌一份 Mock 数据。seed 是幂等的，重复跑不会出问题。
if not exist "data\ecom.db" (
    echo [提示] 没找到 data\ecom.db，先生成一份 Mock 数据（约需十几秒）...
    .venv\Scripts\python.exe -m scraper.seed --count 1000
    echo.
)

echo 正在启动采集 worker...
rem ★ 这里【不加】--replay：--replay 是"灌一次数据就退出"，
rem   进了那条分支就不会进轮询循环——既不会认领任务，也不会检查告警。
rem   所以它给不了上面那些承诺。要灌真实数据请按提示另开窗口跑。
rem   （这条是写脚本时实测出来的：默认参数的 worker --once 能认领并跑完
rem     mock 任务，而 --replay 打印完"重放完成"就直接退出了。）
rem ★ 新窗口要自己再设一次编码：chcp 是每个控制台各自的状态，
rem   不会被子进程继承。少了这段，worker 的中文日志在那边可能是乱码。
start "ecom-intel worker" cmd /k "chcp 65001>nul && .venv\Scripts\python.exe -m scraper.worker"

rem 等 worker 把库初始化好再起 Web，避免两个进程同时建表。
timeout /t 2 /nobreak >nul

echo 正在启动 Web 服务...
echo.
rem ★ 绑 127.0.0.1 而不是 0.0.0.0：不给局域网开门。
rem   要用手机看的话再改成 0.0.0.0，同时记得看一眼 Windows 防火墙提示。
.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8001

pause
