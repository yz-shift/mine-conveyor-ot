# PROGRESS — mine-conveyor-ot

> 进度日志。新的写在最上面；只追加，不删改旧记录。

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
