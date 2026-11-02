# PROGRESS — mine-conveyor-ot

> 进度日志。新的写在最上面；只追加，不删改旧记录。

## 2026-11-02 · T-005 · 完成
- 做了：HMI 后端。报警引擎（纯逻辑：阈值、死区、延时、确认、四状态）；12 条报警配置；SQLite 记录报警事件和操作（失败的操作也记）；Modbus 客户端（每个表一次读完，断线自动重连）；每秒轮询服务；REST 接口和 WebSocket 推送。断连时读数返回 null 而不是冻结的旧值，并触发 comm_lost 报警，其他报警保持原状态。轮询循环崩溃时整个进程退出。为了给 T-006 直接用，REST 和 WebSocket 接口也在这一步做了
- 改了：hmi/alarms.py、hmi/logbook.py、hmi/plant_client.py、hmi/service.py、hmi/app.py、hmi/__init__.py、config/alarms.yaml、tests/test_alarms.py、tests/test_hmi.py（新增）；shared/settings.py、tests/test_settings.py、.env.example（加 HMI_DB_PATH）；config/plant.yaml（overload_factor 1.8 → 2.0）
- 验证：`pytest` 全部通过，连跑 3 次无偶发失败（当时 155 项，之后加 1 项配置测试）；全链路测试用真实 Modbus、HTTP、WebSocket 和 SQLite 文件。故意改坏 32 处：第一轮发现 27 处，漏掉的 5 处都是测试断言太松，补测试后这 5 处全部能发现。用真实进程和默认参数跑过：先起 HMI 后起模拟器，再做启动、打滑、过载、急停、杀掉模拟器，报警和数据库记录都符合预期
- 问题：① 第一版阈值是估算的，测试发现满速健康运行也会误报（电流实际最高 393 A，注释写的约 340 A）。改成用 20 小时模拟数据定阈值，并加了"6 小时健康运行无误报"测试。过载和满速的波动有重叠，所以把过载倍数调到 2.0。② 不加新依赖，所以测试不用 TestClient（要 httpx），直接起 uvicorn。③ 已知局限，写进 T-009：接口没有认证，也没有防跨站请求，只适合本机；报警状态在内存里，HMI 重启后会重新判断；"操作成功"只代表 Modbus 写入成功，设备是否接受要看 run_state；电机低于 0.5 m/s 不算打滑；有些故障在标称速度下只到警告级，比如过载通常到不了严重级
- 下一步：T-006 HMI 网页

## 2026-11-02 · T-004 · 完成
- 做了：写 Modbus TCP 服务端，按 PLC 扫描周期运行（读控制点 → 推进物理模型 → 发布读数，每 100 ms 一次）；命令线圈执行后自动清零，故障注入线圈保持电平；同一扫描内多条命令时急停优先于停止，停止优先于启动；超范围设速钳位后写回寄存器；扫描循环崩溃时进程退出，不继续提供冻结的读数
- 改了：simulator/server.py（新增）、tests/test_server.py（新增）、shared/registers.py（加 DEVICE_ID）
- 验证：`pytest` 93 项全部通过（连跑 3 次无偶发失败），其中 20 项会起真实 TCP 服务。另外在临时副本里故意改坏 15 处：第一轮 14 处里发现 13 处，漏掉的"监听前先发布读数"已补测试；再加一处"扫描中途让出事件循环"，最终 15/15 发现。还用 `python -m simulator.server` 起独立进程、默认参数实测：3 秒爬升到 1.47 m/s，打滑时皮带 1.41、电机 2.0，急停后 fault_code=12（打滑 + 急停）
- 问题：① Windows 上 pymodbus 用 SO_REUSEADDR 监听，第二个服务端占同一端口不报错，已加启动前的端口检查和测试；Linux 分支未在本机验证。② pymodbus 按 16 位一组存线圈，所以地址 7–15 的线圈可读写但没有作用；它还要求离散输入表非空，所以有一个没人用的占位表。这两点写进 T-009 的已知局限。③ Modbus 本身没有认证，所以只监听本机或 docker 内网
- 下一步：T-005 HMI 后端

## 2026-11-02 · T-003 · 完成
- 做了：写传送带物理模型（状态机、速度爬升、物料、电流、轴承温度、振动、传感器噪声）和四种故障（过载、轴承过热、皮带打滑、急停）；参数放进 config/plant.yaml
- 改了：config/plant.yaml、simulator/plant.py、tests/test_plant.py
- 验证：`pytest` 全部通过。另外在临时副本里故意改坏 14 处代码，第一轮发现 13 处；漏掉的"停机后轴承仍热时振动应为 0"已补测试
- 问题：和 PLAN.md 有一处不同：电流公式改成"空载 + a×速度 + b×流量"，不是方案里的"k×流量×速度"。提升物料的功率取决于吨每小时，流量本身已含速度，再乘一次速度会高估
- 下一步：T-004 Modbus 服务端

## 2026-11-02 · T-002 · 完成
- 做了：建 venv、装依赖并锁定版本；写寄存器表（8 个输入寄存器、1 个保持寄存器、7 个线圈）和校验加载器；写环境变量配置加载
- 改了：pyproject.toml、requirements-lock.txt、.gitignore、.gitattributes、.env.example、config/registers.yaml、shared/registers.py、shared/settings.py、tests/test_registers.py、tests/test_settings.py；AGENTS.md 第 1、2 节（填命令）
- 验证：`pytest` 20 项全部通过，含地址重复、名称重复、超范围、定点数往返、超量程饱和
- 问题：无。设计取舍：超量程时编码饱和而不是报错，和真实传感器一致；每个表的地址空间独立，同一地址可以出现在不同表
- 下一步：T-003 物理模型 + 故障注入

## 2026-11-02 · T-001 · 完成
- 做了：我确认方案全部按推荐执行：Modbus 用 5020 端口；本地 `python run.py` 一条命令启动，docker-compose 交给 GitHub Actions 云端验证；网页用 WebSocket 推送；先集中做完本项目，再做项目 4、5。已拆成具体任务
- 改了：TASKS.md
- 验证：无代码
- 问题：无
- 下一步：T-002

## 2026-10-31 · T-001 · 进行中
- 做了：写好 PLAN.md（架构、技术选型、目录结构、分步计划、待确认问题）
- 改了：PLAN.md（新增）、TASKS.md、AGENTS.md（填了项目说明和特有规则）
- 验证：无代码。本机检查：没有 docker、gcc、make、WSL
- 问题：见 PLAN.md 第 6 节
- 下一步：等我回复 PLAN.md 第 6 节的 4 个问题

## 2026-10-31 00:43 · 初始化 · 完成
- 做了：用模板创建项目，生成 AGENTS.md / TASKS.md / PROGRESS.md
- 改了：无代码
- 验证：无
- 问题：无
- 下一步：填 AGENTS.md 第 1、2 节，然后做 T-001
