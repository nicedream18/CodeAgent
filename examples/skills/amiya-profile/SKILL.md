---
name: amiya-profile
description: 显式选择的教学示例，研究阿米娅并制作附来源的人物介绍页。
tools:
  - FileReadTool
  - FileWriteTool
  - FileEditTool
  - GrepTool
permissions:
  - filesystem_read
  - filesystem_write
---

# 阿米娅介绍页示例

本 Skill 仅在用户明确要求阿米娅介绍页并选择它时使用，不影响其他编程任务。

1. 使用 planner/searchAgent 已提供的研究结论和真实来源；没有证据时先交回 planner 补充研究，不编造 URL。
2. 创建工作区内 `amiya_profile.html`，包含“阿米娅”和“明日方舟”，介绍身份、性格、能力与剧情角色，并说明可能的剧透。
3. 制作自包含、响应式、语义化 HTML，包含至少两个与正文对应的真实来源链接。
4. 通过 FileReadTool 补读生成文件，逐项检查上述要求；在当前 Skill 未开放的 Shell、网页和 MCP 能力需要交回调用者处理。
5. 汇报文件路径、已验证内容及尚未验证的浏览器视觉效果。

教学任务示例：加载 amiya-profile Skill，使用已收集来源制作阿米娅人物介绍页，并读取检查结果。额外的验收数据和非交互命令示例位于 `examples/amiya_plan.py`。
