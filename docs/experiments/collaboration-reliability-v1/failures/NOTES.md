# 开发中失败与修正

- 初次全量出现 8 失败、521 通过、2 跳过：工作区临时目录的 /var 与 /private/var 别名未规范化；旧 Verifier 夹具重复同一行触发新规则。前者规范路径，后者改为实际逐行读取，保留收尾行为验收。该轮原 stdout 使用临时通用日志名，未作为完整原始日志归档，本记录来自当时检查回执。
- pytest-be523f.log：Docker 必须用例由 2 个增加到 3 个，报告测试夹具硬编码计数失败；改为按 REQUIRED 清单计算。
- pytest-0df6b7.log：Verifier 纯文字进展回归夹具未先初始化 guard scope，第一次正常初始化清空了夹具设定的轮数；夹具改为 sync_guard 后，实际文字不能重置无进展计数。
- volatile-log-reference/：真实 Docker 新增重复失败断言时，33 通过、1 失败。除容器 ID 外，stdout_full_output_path / stderr_full_output_path 也会变化，导致结果被误当新证据。统一排除前缀日志引用；同一真实命令三次失败、第四次在创建容器前被拦截。最终通过记录位于 ../../docker/。

上述开发记录不计为模型任务成绩；失败未删除，最终回归独立保存。
