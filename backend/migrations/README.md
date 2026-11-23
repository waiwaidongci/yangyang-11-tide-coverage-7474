# 数据表初始化

本版通过 `src.models.base.Base.metadata.create_all` 创建首次运行所需的表。模型在 `src/models/workflow.py`，除台账、分析、签核与分发外，新增两张表：

- `window_corrections`：潮窗更正，一份可含多个分区的可作业/禁作业时段；`idempotency_key` 唯一，重复提交复用。
- `shift_reports`：班报版本，按 `(work_date, shift, signature)` 唯一；`signature` 为所用更正集合的摘要，`watermark` 为覆盖到的最大更正编号，`is_current` 标记班报页面的当前版。

启动时会为每个已有班次安排一版不含更正的基线班报，由 worker 补算。`python -m scripts.seed_demo` 按观测内容键补全演示样本，保留现有分析、更正与签核。数据库端口只发布到本机。
