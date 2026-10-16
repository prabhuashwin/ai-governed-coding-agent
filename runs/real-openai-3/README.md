# real-openai-3

- run: https://github.com/prabhuashwin/ai-governed-coding-agent/actions/runs/38898631942
- commit: d0e01d2182ad5ed2658b79b658556e523aff85ed

# Eval results — 3/3 passed (100%)

| task | run | result | stop | turns | tools | tokens in/out | cost | secs | Δlines | flags |
|---|---|---|---|---|---|---|---|---|---|---|
| 01-off-by-one-mean | 0 | PASS | finished | 7 | 7 | 9004/211 | $0.0039 | 8.7 | +1/−1 | - |
| 02-slugify-edge-cases | 0 | PASS | finished | 7 | 6 | 9517/457 | $0.0045 | 8.6 | +6/−1 | - |
| 03-inventory-none-handling | 0 | PASS | finished | 11 | 10 | 16043/415 | $0.0071 | 10.3 | +5/−1 | - |

Total cost $0.0155; mean turns 8.3; provider openai_compatible/gpt-4.1-mini