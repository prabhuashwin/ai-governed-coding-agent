# Fix mean() off-by-one

Job `job_0fb852c7e8b3` · branch `gca/job_0fb852c7e8b3` · status **succeeded**
Requested by ashwin; approved by tech-lead; provider scripted/scripted-v1

## Verdict
- Independent verification: **PASSED** (exit 0)
- Agent stop reason: `finished`; agent claimed success: True

## Acceptance criteria
- [ ] mean() returns the arithmetic mean
- [ ] mean([]) still raises ValueError
- [ ] all tests pass

## Agent summary (unverified)
mean() divided by len(values) - 1, an off-by-one error. Changed the divisor to len(values). The empty-list ValueError is unchanged. All 4 tests pass.

## Changes
1 file(s), +1 −1
- `stats.py`

## Test evidence (verifier run, clean checkout)
```
....                                                                     [100%]
4 passed in 0.01s
```

## Cost
turns 5 · tool calls 4 · tokens in/out 5000/500 · $0.0225 · 1s

## Audit
Hash-chained audit log: `.gca/audit/job_0fb852c7e8b3.jsonl` (verify with `gca audit job_0fb852c7e8b3 --verify`).

_Merge, release and production actions remain separate human approvals._

## Diff
```diff
diff --git a/stats.py b/stats.py
index 0243cdb..a37da80 100644
--- a/stats.py
+++ b/stats.py
@@ -4,7 +4,7 @@
 def mean(values):
     if not values:
         raise ValueError("mean of empty list")
-    return sum(values) / (len(values) - 1)
+    return sum(values) / len(values)
 
 
 def median(values):

```

