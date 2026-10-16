# real-openai-4

- run: https://github.com/prabhuashwin/ai-governed-coding-agent/actions/runs/38898799134
- commit: d0e01d2182ad5ed2658b79b658556e523aff85ed

# Eval results — 9/9 passed (100%)

| task | run | result | stop | turns | tools | tokens in/out | cost | secs | Δlines | flags |
|---|---|---|---|---|---|---|---|---|---|---|
| 01-off-by-one-mean | 0 | PASS | finished | 8 | 7 | 9830/208 | $0.0043 | 7.9 | +1/−1 | - |
| 01-off-by-one-mean | 1 | PASS | finished | 8 | 7 | 10118/356 | $0.0046 | 9.2 | +1/−1 | - |
| 01-off-by-one-mean | 2 | PASS | finished | 9 | 8 | 12247/232 | $0.0053 | 8.4 | +1/−1 | - |
| 02-slugify-edge-cases | 0 | PASS | finished | 8 | 7 | 10933/536 | $0.0052 | 9.1 | +4/−1 | - |
| 02-slugify-edge-cases | 1 | PASS | finished | 8 | 7 | 10618/426 | $0.0049 | 8.9 | +4/−2 | - |
| 02-slugify-edge-cases | 2 | PASS | finished | 6 | 5 | 8204/488 | $0.0041 | 8.0 | +6/−1 | - |
| 03-inventory-none-handling | 0 | PASS | finished | 14 | 13 | 22344/457 | $0.0097 | 12.2 | +6/−1 | - |
| 03-inventory-none-handling | 1 | PASS | finished | 10 | 9 | 14641/319 | $0.0064 | 9.0 | +4/−1 | - |
| 03-inventory-none-handling | 2 | PASS | finished | 15 | 14 | 24028/714 | $0.0108 | 14.5 | +7/−1 | - |

Total cost $0.0553; mean turns 9.6; provider openai_compatible/gpt-4.1-mini