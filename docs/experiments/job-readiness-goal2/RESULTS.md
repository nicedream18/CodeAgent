# Goal 2：真实固定任务对照结果

已归档终态：18/18。完整矩阵：True。

完整交付要求运行正常结束且独立可信测试通过；失败后的产物通过另列。

| 版本 | 完整交付 / 固定分母 | 产物通过（含部分） | 请求 | 工具 | 已报告 token | 基础设施失败 |
|---|---|---|---|---|---|---|
| B0 | 1/9 | 8 | 208 | 238 | 1656405 | 1 |
| B1 | 0/9 | 8 | 219 | 248 | 1693752 | 0 |

| 顺序 | 版本 | 任务 / 重复 | 运行终态 | 独立通过 | 完整交付 | 请求 | token | 停止原因 |
|---|---|---|---|---|---|---|---|---|
| 1 | B1 | M02-v2 / 3 | failed | True | False | 22 | 188684 | token_budget_exhausted |
| 2 | B1 | M08-v2 / 2 | failed | False | False | 22 | 184171 | token_budget_exhausted |
| 3 | B1 | M08-v2 / 1 | failed | True | False | 25 | 187464 | token_budget_exhausted |
| 4 | B0 | M08-v2 / 1 | finished | True | True | 24 | 192830 | — |
| 5 | B0 | M07-v2 / 1 | failed | False | False | 25 | 186122 | token_budget_exhausted |
| 6 | B1 | M07-v2 / 1 | failed | True | False | 27 | 187402 | token_budget_exhausted |
| 7 | B0 | M07-v2 / 2 | failed | True | False | 23 | 185750 | token_budget_exhausted |
| 8 | B0 | M07-v2 / 3 | failed | True | False | 25 | 192936 | token_budget_exhausted |
| 9 | B1 | M02-v2 / 1 | failed | True | False | 23 | 187563 | token_budget_exhausted |
| 10 | B1 | M07-v2 / 2 | failed | True | False | 27 | 193518 | token_budget_exhausted |
| 11 | B1 | M08-v2 / 3 | failed | True | False | 22 | 182462 | token_budget_exhausted |
| 12 | B0 | M08-v2 / 2 | failed | True | False | 23 | 194085 | token_budget_exhausted |
| 13 | B0 | M02-v2 / 2 | failed | True | False | 22 | 188480 | token_budget_exhausted |
| 14 | B1 | M02-v2 / 2 | failed | True | False | 24 | 189238 | token_budget_exhausted |
| 15 | B0 | M02-v2 / 3 | infrastructure_failed | True | False | 17 | 138879 | provider_APIConnectionError |
| 16 | B1 | M07-v2 / 3 | failed | True | False | 27 | 193250 | token_budget_exhausted |
| 17 | B0 | M02-v2 / 1 | failed | True | False | 25 | 193794 | token_budget_exhausted |
| 18 | B0 | M08-v2 / 3 | failed | True | False | 24 | 183529 | token_budget_exhausted |

| 任务 | 版本 | 完整交付 | 请求 min / median / max | token min / median / max | 生成秒 min / median / max |
|---|---|---|---|---|---|
| M02-v2 | B0 | 0/3 | 17 / 22 / 25 | 138879 / 188480 / 193794 | 46.058 / 54.275 / 56.928 |
| M02-v2 | B1 | 0/3 | 22 / 23 / 24 | 187563 / 188684 / 189238 | 54.345 / 57.268 / 61.348 |
| M07-v2 | B0 | 0/3 | 23 / 25 / 25 | 185750 / 186122 / 192936 | 57.355 / 60.425 / 64.341 |
| M07-v2 | B1 | 0/3 | 27 / 27 / 27 | 187402 / 193250 / 193518 | 71.346 / 71.809 / 73.711 |
| M08-v2 | B0 | 1/3 | 23 / 24 / 24 | 183529 / 192830 / 194085 | 73.628 / 126.447 / 128.853 |
| M08-v2 | B1 | 0/3 | 22 / 22 / 25 | 182462 / 184171 / 187464 | 62.915 / 74.237 / 77.283 |

上表 token 为供应商返回或已明确标记的估算；失败连接的未知实际计费量不能由这些值反推。

每任务三次的 min/median/max、成功与失败耗时、角色开销、缓存未知量及验收明细见 comparison.json。
显式模型自评按最终汇报标记解析；未出现时记未知。内部 verifier 与外部测试分别记录。
预算停止保留文件产物，不能等同于完整交付。成功率提升不是本批完成的必要条件。

- 18 fixed trials, 3 repetitions per version/task, no broad generalization
- Historical 0/56 remains unchanged; no official SWE-bench score
- Cache reads included in input; unknown creation usage is not zero
- Runtime worker claim may conflate verifier; parsed claims reported separately
- Token budget uses estimates/reservation, not a precise billing hard limit
- Failure and delivery durations have separate distributions
