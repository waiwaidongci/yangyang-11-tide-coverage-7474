# 沙岸保洁·潮线班次覆盖口径

把清扫轨迹交并到分区潮窗内，对比纯分钟覆盖与岸线长度加权覆盖，并列出重叠和越界供班长签核。

## 模拟场景

[业务剧本](yangyang-business.md)记录模拟团队、上游观测、统计口径和交接去向。四个工作模块包括数据台账、分析任务、结果与复核、报表分发；「班报与更正」页面集中处理潮窗更正和版本化班报。数据从合成种子或 UTF-8 CSV 进入。分析员提交后，Python worker 对固化观测清单计算。负责人签核，随后可以下载结果或把报告定时投递到本地收件箱。

## 潮窗更正与班报版本

- 一份潮窗更正可同时划定多个分区的「可作业」或「禁作业」时段。同一分钟以最新签发的更正为准，未被更正覆盖的时段继续沿用原班报潮窗。
- 更正提交后，worker 用原班报的完工轨迹重算覆盖率与异常，整版算齐后才切换班报页面的当前版。算齐之前，页面继续显示上一版当前版。
- 旧版本和每版采用的更正来源都保留可查。多份更正可一起提交；先到的更正即使补算更晚，也不会让班报退回旧版。未被后续更正覆盖的更正时段照样入账。
- 补算失败可「重试补算」，重试复用同一版本、不会重复生成班报。整班潮窗全部撤为禁作业时，窗外完工仍逐段列出，覆盖率留空。

## 启动

```sh
cp .env.example .env
docker compose up -d --build --wait
docker compose exec -T backend python -m scripts.seed_demo
```

浏览器打开 `http://127.0.0.1:18380`。API 健康检查位于 `http://127.0.0.1:19380/api/health`，数据库本地端口 `17880`。停机用 `docker compose down`。只有要重做演示数据时才加 `-v` 清除命名卷。

## 演示账号

| 角色 | 邮箱 | 演示密码 | 可执行动作 |
|---|---|---|---|
| 业务分析员 | `analyst@demo.local` | `Analyst-2026!` | 登记或导入样本、提交分析、提交潮窗更正、重试班报补算、下载内部结果 |
| 复核负责人 | `supervisor@demo.local` | `Supervisor-2026!` | 导入资料、提交潮窗更正、重试班报补算、签核或退回、安排或停止分发、查审计 |
| 只读查看 | `viewer@demo.local` | `Viewer-2026!` | 查看台账、已签核结果、当前班报与本地收件箱 |

`.env` 以 `.env.example` 为模板。`FRONTEND_PORT`、`BACKEND_PORT`、`DB_PORT` 可做本机端口调整。Docker 私有子网可通过 `NETWORK_SUBNET` 覆盖。`JWT_SECRET` 和 `DB_PASSWORD` 为本地演示配置。`SEED_DEMO=false` 可跳过自动种子。所有公开端口绑定在回环地址。首次执行种子后，后续种子只填缺失观测，不覆盖已录入数据。

## 模块和本地开发

| 服务 | 技术 | 用途 |
|---|---|---|
| frontend | HTML、CSS、ES Modules、Nginx | 工作台、录入、班报与更正、报告 |
| backend | Python 3.12、FastAPI、SQLAlchemy | 校验、快照、潮窗更正、版本化班报、业务 API 和 JWT |
| worker | Python 3.12、Redis | 计算排队、心跳和到时导出 |
| db | PostgreSQL 16 | 观测、快照、签核、分发计划和审计 |
| redis | Redis 7 | 唤醒、心跳和登录限流 |

在 `backend` 中可用 `pip install -r requirements.txt` 安装本地依赖，设置 `DATABASE_URL`、`REDIS_URL` 和可写 `ARTIFACT_ROOT` 后运行 `uvicorn src.main:app --port 8000`，计算进程运行 `python -m src.worker`。前端修改在 `frontend/src`，用 `docker compose up -d --build frontend` 通过 Nginx 检查同源代理。业务输入定义在 `backend/src/domain/spec.json`，算法在同目录的 `analysis.py`。角色、任务状态和签核状态在后端 `backend/src/constants/enums.py` 和前端 `frontend/src/constants/enums.js` 共同定义。数据库表在 `backend/src/models/workflow.py`。

分发计划锁定特定已签核快照，按北京时间显示。收件箱可下载真实 CSV，当前收件组是本地交接标签。停机跨过多个周期时补送最早未投递的一期，然后追到下一个将来的周期。新快照需重新签核并建新计划。

许可证为 [MIT](LICENSE)。
