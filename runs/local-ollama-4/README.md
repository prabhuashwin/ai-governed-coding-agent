# local-ollama-4

- run: https://github.com/prabhuashwin/ai-governed-coding-agent/actions/runs/38901066068
- commit: 8cb5f8fee95ce040b19f30786af8ec656ee7154c

# Eval results — 1/3 passed (33%)

| task | run | result | stop | turns | tools | tokens in/out | cost | secs | Δlines | flags |
|---|---|---|---|---|---|---|---|---|---|---|
| 01-off-by-one-mean | 0 | PASS | finished | 9 | 8 | 16668/831 | $0.0 | 308.3 | +1/−1 | - |
| 02-slugify-edge-cases | 0 | FAIL | finished | 12 | 9 | 30697/946 | $0.0 | 472.2 | +1/−1 | - |
| 03-inventory-none-handling | 0 | FAIL | finished | 14 | 12 | 38678/1806 | $0.0 | 1586.3 | +3/−4 | - |

Total cost $0.0000; mean turns 11.7; provider openai_compatible/qwen2.5:7b