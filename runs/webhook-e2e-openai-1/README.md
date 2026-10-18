# webhook-e2e-openai-1

- run: https://github.com/prabhuashwin/ai-governed-coding-agent/actions/runs/39119462523
- commit: 9e95610eb0ebc5b239c91b01db59229b11112e78

# Webhook E2E — PASS (openai/gpt-4.1-mini)

| check | result |
|---|---|
| unsigned_command_rejected | ok |
| submit_accepted | ok |
| all_signatures_valid | ok |
| no_callback_errors | ok |
| lifecycle_in_order | ok |
| approved_by_platform | ok |
| job_succeeded | ok |
| verified_independently | ok |
| audit_hash_present | ok |
| status_endpoint_agrees | ok |
| kill_switch_round_trip | ok |

Events seen by the platform: job.submitted, approval.requested, approval.decided, job.started, job.verified, job.finished
Finished: {'status': 'succeeded', 'error': None, 'branch': 'gca/job_592aa7362ecd', 'model': 'gpt-4.1-mini'}
Usage: {'input_tokens': 8982, 'output_tokens': 215, 'cost_usd': 0.0039368, 'turns': 7, 'tool_calls': 6, 'seconds': 9.385598796000004}
