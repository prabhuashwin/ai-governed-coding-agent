# real-openai-5

- run: https://github.com/prabhuashwin/ai-governed-coding-agent/actions/runs/38898811484
- commit: d0e01d2182ad5ed2658b79b658556e523aff85ed

# Eval results — 3/3 passed (100%)

| task | run | result | stop | turns | tools | tokens in/out | cost | secs | Δlines | flags |
|---|---|---|---|---|---|---|---|---|---|---|
| 01-off-by-one-mean | 0 | PASS | finished | 6 | 7 | 8117/223 | $0.018 | 7.5 | +1/−1 | - |
| 02-slugify-edge-cases | 0 | PASS | finished | 8 | 9 | 13017/408 | $0.0293 | 7.8 | +7/−1 | - |
| 03-inventory-none-handling | 0 | PASS | finished | 12 | 11 | 18229/363 | $0.0394 | 8.2 | +4/−1 | - |

Total cost $0.0867; mean turns 8.7; provider openai_compatible/gpt-4.1