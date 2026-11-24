# 矩陣狀態（phase1-l2）

- 狀態：**完成（2026-11-24 補算 V5／V6／副作用後更新）**
- 更新時間：2026-11-24 18:48:04

- 有效 trial：9，通過 9；無效（529／限流等）：3

| trial | 模型 | 有效 | PASS | V4 | V5 | V6 | 時間（分） | 費用（估） |
|---|---|---|---|---|---|---|---|---|
| 20261123-163956 | opus-5-5 | 是 | True | True | True | True | 30.6 | $2.52 |
| 20261123-171516 | sonnet-5-5 | 否（api_overloaded） | False | False | False | False | 25.4 | $1.27 |
| 20261123-174646 | sonnet-5-5 | 否（api_overloaded） | False | False | True | False | 3.2 | $0.01 |
| 20261123-191640 | sonnet-5-5 | 是 | True | True | True | True | 29.6 | $1.62 |
| 20261123-195049 | haiku-5-5 | 是 | True | True | True | True | 32.0 | $2.27 |
| 20261123-202743 | opus-5-5 | 是 | True | True | True | True | 27.9 | $3.87 |
| 20261123-210015 | sonnet-5-5 | 是 | True | True | True | True | 33.0 | $1.97 |
| 20261123-213826 | haiku-5-5 | 是 | True | True | True | True | 22.9 | $1.02 |
| 20261123-220627 | opus-5-5 | 是 | True | True | True | True | 14.1 | $2.96 |
| 20261123-222517 | sonnet-5-5 | 是 | True | True | True | True | 30.0 | $1.40 |
| 20261123-230006 | haiku-5-5 | 否（rate_limited） | True | True | True | True | 18.6 | $1.03 |
| 20261124-164316 | haiku-5-5 | 是 | True | True | True | True | 24.9 | $1.27 |

## matrix.log（這次啟動之後）

```
[2026-11-24 16:43:16] matrix start task=L2 rounds=3 skip=8 models=claude-opus-5-5 claude-sonnet-5-5 claude-haiku-5-5
[2026-11-24 16:43:16] round 3 model claude-haiku-5-5
[2026-11-24 17:13:06] rc=0 RESULT L2-normal-haiku-5-5-high-20261124-164316: pass=True strict=True (V1=True V2=True V3=True V3x=True V4=True req=True setup_untouched=True V5=None V6=True) cost=$1.27045209 (est.) turns=111 wall=1491s exit=0 valid=True  side_effe
[2026-11-24 17:13:06] matrix done
```
