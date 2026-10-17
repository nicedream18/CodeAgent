"""Create an explicitly labelled 150-second replay from archived observations."""

import argparse
import json
import re
import tarfile
from pathlib import Path


def safe(text):
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(text))
    return "".join(c for c in text if c in "\n\t" or ord(c) >= 32)


parser = argparse.ArgumentParser()
parser.add_argument("trial_id")
parser.add_argument("--archive", type=Path, required=True)
parser.add_argument("--output", type=Path, required=True)
args = parser.parse_args()
if not re.fullmatch(r"[A-Za-z0-9_-]+", args.trial_id):
    raise SystemExit("Invalid trial ID")
trial = args.archive / "trials" / args.trial_id
result = json.loads((trial / "result.json").read_text())
if not result["grade"]["solved"]:
    raise SystemExit("Demo requires an independently passing artifact")
task = json.loads(
    (args.archive.parent / "tasks" / (result["config"]["task_id"] + ".json")).read_text()
)
complete = result["execution_status"] == "finished" and result["task_solved"]
final_report = ""
repository_evidence = ""
if complete:
    with tarfile.open(trial / "trace.tar.gz") as archive:
        handle = archive.extractfile(".mokioclaw/checkpoints/checkpoint.json")
        if handle is not None:
            summary = json.loads(handle.read(1024 * 1024))["summary"]
            final_report = summary.get("final_answer", "")
            repository_evidence = summary.get("repo_context", "")
label = "完整交付" if complete else "产物通过、运行失败：诊断回放"
frames = [
    (0, f"MokioAgent | {label}\n{args.trial_id}\n这是已归档记录的回放，不是一次新的实时模型运行。\n"),
    (12, "需求与来源\n" + task["requirement"][:1500] + "\nbase: " + task["base_commit"]),
    (32, "仓库证据（checkpoint 摘要）\n" + (repository_evidence[:2200] or "\n".join(
        json.loads((trial / "file-changes.json").read_text())))
     + "\n完整模型请求、读取与调用证据保存在同一 trial 的 trace.tar.gz。"),
    (52, "实际候选 diff\n" + (trial / "candidate.patch").read_text()[:6000]),
    (90, "独立容器验收（可信测试重建）\n" + json.dumps({
        k: result["grade"][k] for k in (
            "valid", "solved", "target_passed", "target_failed", "regression_failed", "tests_run"
        )
    }, ensure_ascii=False, indent=2)),
    (112, "原运行状态与用量\n" + json.dumps({
        "run_status": result["execution_status"],
        "task_solved": result["task_solved"],
        "stop_reason": result["stop_reason"],
        "requests": result["model_metrics"].get("requests"),
        "run_id": result["run_id"],
    }, ensure_ascii=False, indent=2)),
    *([(124, "实际最终汇报（checkpoint 有界摘要）\n" + final_report[:1500])] if final_report else []),
    (132, "结论边界\n" + (
        "这是一份正常完成且独立验收通过的候选。"
        if complete else
        "仅部分产物通过测试。原运行没有正常完成，主口径仍为未解决。\n不能把这段回放当作端到端成功演示。"
    ) + "\n" + task["source"] + "\n单例演示不替换原架构对照成绩。"
      + "\n原始失败、基础设施问题和所有预定试验均保留。"),
    (150, "回放结束。复现已有产物不会再次请求模型。\n"),
]
args.output.parent.mkdir(parents=True, exist_ok=True)
with args.output.open("w") as handle:
    handle.write(json.dumps({"version": 2, "width": 110, "height": 36,
                             "title": f"MokioAgent {args.trial_id}: {label}"}, ensure_ascii=False) + "\n")
    for timestamp, text in frames:
        handle.write(json.dumps([timestamp, "o", "\x1b[2J\x1b[H" + safe(text) + "\n"], ensure_ascii=False) + "\n")
print(json.dumps({"output": str(args.output), "duration_seconds": 150, "complete_delivery": complete}))
