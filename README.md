# 业务窗关账

> 遵守 `ops/底座生成前必读_效率与技术栈.md`（**≥3× 厚度**）。  
> FastAPI + Vue 3 + SQLite；交付须 `docker compose up -d --build` 一键起。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立。

## 骨架一句话

事件按业务窗裁切，迟到进旁路或拒收，关窗后物化结果并与窗口对账。

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 业务窗关账 |
| 目录 | `08-windowcut` |
| 接口 | http://127.0.0.1:11018 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 厚度（相对薄壳 ≥3×）

| 项 | 目标 |
|----|------|
| 业务 LOC | ≥900（目标 1000+） |
| 后端具名模块 | `db` / `auth` / `locks` / `windows_svc` / `ingest_svc` / `late_svc` / `materialize_svc` / `reconcile_svc` / `audit_svc` / `hold_svc` / `reopen_svc` |
| 前端视图 | 业务窗 / 入账 / 迟到桶 / 物化 / 对账 / 审计争用 |
| 空桩 | hold 晋升、迟到补入、分区物化、窗重开 |

## 为何能出多题

开窗关窗、迟到策略、窗重开、部分物化失败回滚、旁路暂存 vs 拒收、争用锁与审计。

## 互斥切口候选

- 迟到拒收 vs 进迟到桶
- 物化失败整窗回滚 vs 保留成功分区
- 窗重开作废重算 vs 增量补丁合并
- 切版旁路暂存 vs 开始瞬间拒收

## 迟到归属（甲路径，当天锁定，见 `docs/迟到归属册.md`）

迟到桶曾按 `window_name` 字符串汇总，删窗后桶行成孤儿、同名新窗会跨窗串桶，摘要卡与明细表漂移。
本仓选定**甲路径**并写入 `late_attribution_registry`（当天禁改口，偏离拒写并记 `late_attribution_drift`）：

- `late_bucket.window_id` 为 `NOT NULL REFERENCES windows(id) ON DELETE RESTRICT` 存活外键；
- 删窗同事务拒删，或带 `migrate_to` 级联改挂后删窗，冲突整笔回滚；
- 改挂桶行占桶锁 `late-bucket:{id}`，双人同改后到者 409 只记争用；
- 写事务 `BEGIN IMMEDIATE` + `busy_timeout=0`，写锁入口即定；
- 旧库挂卷升级自动回填 `window_id`，匹配不上的进 `late_bucket_quarantine` 检疫，不丢不串；
- 迟到明细 / 汇总卡 / 窗口条带 / 对账迟到计数 / 审计五处同态，终态 `orphan_buckets=0`。

新增接口：`GET/POST /api/late-attribution/registry|choose`、`POST /api/late-bucket/reassign`、
`DELETE /api/windows/{wid}?migrate_to=`、`GET /api/late-bucket/quarantine`。
红测先行见 `tests/test_late_attribution.py`（跨窗串桶/孤儿失败测 + 双人争用失败测）。

## 启动（一键）

```powershell
cd projects/08-windowcut
docker compose up -d --build
```

浏览器：http://127.0.0.1:11018/  
健康检查：GET http://127.0.0.1:11018/api/health

本机开发：

```powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10605
python -m pytest -q
```
