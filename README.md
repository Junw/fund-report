# A 股收盘日报 Web 看板

一个面向个人 NAS 部署的 A 股收盘日报应用。它使用 AKShare 拉取股票、权益类基金、ETF、行业板块、概念板块和市场风险指标，保存到 SQLite，并在收盘后生成 Web 报表。

## 本地运行

```powershell
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m app.main
```

默认访问：`http://127.0.0.1:8088`

## NAS Docker 部署

```sh
mkdir -p /volume1/docker/fund-report
/usr/local/bin/docker build -t fund-report:latest .
/usr/local/bin/docker run -d \
  --name fund-report \
  --restart unless-stopped \
  -p 8088:8088 \
  -v /volume1/docker/fund-report:/data \
  -e TZ=Asia/Shanghai \
  fund-report:latest
```

日常修改 Python、模板或 CSS 后，用快速部署脚本同步源码并重启容器，不会重建依赖层：

```powershell
.\tools\deploy-nas.ps1 -NasHost NAS_IP
```

依赖或 Dockerfile 变化时再重建镜像：

```powershell
.\tools\deploy-nas.ps1 -NasHost NAS_IP -Rebuild
```

也可以使用环境变量：

```powershell
$env:FUND_REPORT_NAS_HOST="NAS_IP"
$env:FUND_REPORT_NAS_USER="root"
.\tools\deploy-nas.ps1
```

## 手动触发任务

```sh
curl -X POST 'http://NAS_IP:8088/api/jobs/run?job=close'
curl -X POST 'http://NAS_IP:8088/api/jobs/run?job=fund_refresh'
```

## 重要说明

页面中的规则化建议仅供个人研究记录，不构成投资建议。AKShare 的公开接口可能随上游站点调整而变化，任务失败时请先查看 `/jobs` 页面中的错误信息。

## 中期选基研究

- `/research`：实验信号、因子分数、数据积累进度和最新回测。
- `/watchlist`：网页观察池。
- 历史净值、基金资料和季度持仓任务采用限速批次与断点续传；需要在 `/jobs` 多次运行，部署不会自动一次性抓取全市场三年数据。
- `signal_compute` 只生成实验信号；回测未达到门槛前不会替换首页现有推荐。
- 回测不足 24 个月时明确显示历史不足，不会使用未来披露数据补齐。
