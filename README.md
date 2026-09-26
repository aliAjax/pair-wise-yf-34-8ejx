# 无人机飞行计划审批与空域协调系统

标准库独立项目。系统记录运营方计划、航线、载荷、高度、人口风险和应急方案。航线按航点拆为连续航段，每段记录起止高度，逐段核对禁飞区的水平范围和高度区间；冲突会说明航段编号和限制名称并阻止批准。审核结果支持离线编号幂等回传，已批准计划改动任一航段后原批准自动失效并回到重新提交流程。

## 航段模型

- 航线点支持 `[经度,纬度]` 或 `[经度,纬度,高度]` 两种形式；未带高度的航点按申报的 `max_altitude` 计算，显式航点高度不得超过 `max_altitude`。
- 相邻航点构成一个航段，`GET /api/plans/{id}` 返回的 `segments` 记录每段的起止点和起止高度。
- 冲突检查按航段直线与限制区矩形做水平相交，再比较航段高度区间与限制高度区间，两者都重叠才判冲突，避免整航线包围盒和单一最高高度带来的误报。
- 航段计算在 `segments.py`，与计划接口 `app.py` 分离；协调台页面样式在 `static/console.css` 单独接入。

## 运行

```bash
python3 app.py --db drone_airspace.db
```

默认监听 `127.0.0.1:8205`，首页 `/`（协调台展示航段与冲突位置），健康检查 `/health`。

身份头为 `X-User-Id`、`X-Role`；运营方还需 `X-Operator`。角色：`viewer`、`operator`、`airspace_reviewer`、`commander`、`auditor`。

## 主要接口

- `POST /api/restrictions`：新增临时限制或禁飞区。
- `POST /api/plans`：创建飞行计划。
- `GET /api/plans/{id}/check`：逐段检查硬约束、空域限制和相邻交通冲突，返回航段列表与冲突位置。
- `POST /api/plans/{id}/submit`、`approve`、`reject`：提交和审核；审核使用 `offline_id` 保证断网重连幂等。
- `POST /api/plans/{id}/change`、`cancel`：版本化变更与取消；变更后状态回到 `draft` 需重新提交，已批准计划生成批准失效通知。
- `GET /api/notifications`、`POST /api/expire`：通知与到期处理。
- `GET /api/state`：按角色返回计划、限制和公开信息。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 主要局限

限制区水平范围使用经纬度矩形，航段按直线与矩形精确相交判断，但相邻交通仍用包围盒近似；不包含多边形、椭球距离、地形、实时遥测和完整间隔标准。紧急授权只能覆盖空域及交通冲突，不能绕过载荷与高度硬限制。身份头、无签名离线审核以及单机 SQLite 适合原型，生产环境需要 PKI、真实 GIS 引擎和跨机构事件总线。
