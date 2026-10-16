"""Render the human-review report (doubles as the pull-request description)."""

from __future__ import annotations

from .models import Job, RunResult


def render_report(job: Job, result: RunResult) -> str:
    wi = job.work_item
    a = result.agent
    v = result.verification
    lines: list[str] = []
    lines.append(f"# {wi.title}")
    lines.append("")
    lines.append(f"Job `{job.id}` · branch `{result.branch or '-'}` · status **{job.status.value}**")
    lines.append(f"Requested by {wi.requester}; approved by {job.approval.approver if job.approval else '—'}; "
                 f"provider {result.provider}/{result.model}")
    lines.append("")
    lines.append("## Verdict")
    if v is None:
        lines.append("- Verification did not run.")
    else:
        lines.append(f"- Independent verification: **{'PASSED' if v.passed else 'FAILED'}**"
                     + (f" (exit {v.exit_code})" if v.exit_code is not None else ""))
        if v.flags:
            lines.append("- Flags: " + ", ".join(f"`{f}`" for f in v.flags))
    if a is not None:
        lines.append(f"- Agent stop reason: `{a.stop_reason.value}`; agent claimed success: {a.claimed_success}")
        if a.claimed_success and v is not None and not v.passed:
            lines.append("- ⚠️ The agent claimed success but verification failed. Do not trust the summary below.")
        if a.blocked_actions:
            lines.append(f"- ⚠️ {a.blocked_actions} action(s) were blocked by policy — review the audit log.")
    lines.append("")
    lines.append("## Acceptance criteria")
    for c in wi.acceptance_criteria:
        lines.append(f"- [ ] {c}")
    lines.append("")
    lines.append("## Agent summary (unverified)")
    lines.append(a.summary.strip() if a and a.summary else "_none_")
    lines.append("")
    lines.append("## Changes")
    if v and v.files_changed:
        lines.append(f"{len(v.files_changed)} file(s), +{v.insertions} −{v.deletions}")
        for f in v.files_changed:
            lines.append(f"- `{f}`")
    else:
        lines.append("_no files changed_")
    lines.append("")
    lines.append("## Test evidence (verifier run, clean checkout)")
    lines.append("```")
    lines.append((v.output_tail if v else "").strip() or "(no output)")
    lines.append("```")
    lines.append("")
    if a:
        u = a.usage
        lines.append("## Cost")
        lines.append(f"turns {u.turns} · tool calls {u.tool_calls} · tokens in/out {u.input_tokens}/{u.output_tokens} · "
                     f"${u.cost_usd:.4f} · {u.seconds:.0f}s")
        lines.append("")
    lines.append("## Audit")
    lines.append(f"Hash-chained audit log: `.gca/audit/{job.id}.jsonl` (verify with `gca audit {job.id} --verify`).")
    lines.append("")
    lines.append("_Merge, release and production actions remain separate human approvals._")
    return "\n".join(lines)
