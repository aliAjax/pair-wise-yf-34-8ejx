# 无人机飞行计划审批与空域协调系统

标准库独立项目。系统记录运营方计划、航线、载荷、高度、人口风险和应急方案，检查临时禁飞区、高度范围、人口风险以及相邻有效计划冲突。审核结果支持离线编号幂等回传，计划变更会使原批准失效并生成通知。

航线按连续航段管理：航点支持 `[经度,纬度,高度]`（不给高度时按计划最大高度平飞），相邻航点组成一个航段并记录起止高度与高度区间。限制区检查逐段核对水平范围与高度区间，冲突报告给出航段编号和限制名称并阻止批准；已批准计划改动任一航段（端点经纬度或高度）后状态回到 `draft`，原批准失效，需重新提交。航段计算位于独立模块 `segments.py`（纯计算，无 HTTP/数据库依赖），并通过 `GET /api/plans/{id}/segments` 单独提供；协调台 `/` 展示航段表并高亮冲突航段，样式由独立的 `/styles.css` 接入。

## 运行

```bash
python3 app.py --db drone_airspace.db
```

默认监听 `127.0.0.1:8205`，首页 `/`，健康检查 `/health`，协调台样式 `/styles.css`。

身份头为 `X-User-Id`、`X-Role`；运营方还需 `X-Operator`。角色：`viewer`、`operator`、`airspace_reviewer`、`commander`、`auditor`。

## 主要接口

- `POST /api/restrictions`：新增临时限制或禁飞区。
- `POST /api/plans`：创建飞行计划（航线点可带高度，自动拆分航段）。
- `GET /api/plans/{id}/segments`：查询连续航段及各段起止高度（与计划接口分开）。
- `GET /api/plans/{id}/check`：逐段核对禁飞区/限制区的水平范围和高度区间，返回冲突航段编号、限制名称及其他硬约束、相邻交通冲突。
- `POST /api/plans/{id}/submit`、`approve`、`reject`：提交和审核；审核使用 `offline_id` 保证断网重连幂等，存在航段冲突时批准被阻止（指挥官可紧急授权）。
- `POST /api/plans/{id}/change`、`cancel`：版本化变更与取消；任一航段改动使原批准失效、回到重新提交流程，并生成通知。
- `GET /api/notifications`、`POST /api/expire`：通知与到期处理。
- `GET /api/state`：按角色返回计划（含航段）、限制和公开信息。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

`segments.py` 为纯函数模块，可单独构造航点与限制区验证航段拆分和逐段重叠逻辑（见 `tests/test_segments.py`）。

## 主要局限

空域几何使用经纬度矩形和航段包围盒近似（不做线段与矩形的精确求交），不包含多边形、椭球距离、地形、实时遥测和完整间隔标准。紧急授权只能覆盖空域及交通冲突，不能绕过载荷与高度硬限制。身份头、无签名离线审核以及单机 SQLite 适合原型，生产环境需要 PKI、真实 GIS 引擎和跨机构事件总线。
