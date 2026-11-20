#!/usr/bin/env bash
# 演示：固定底表 -> 提交 v1（C/D 并成一组）-> v2 拆组 -> 按版取变更 -> 旧版重传与冲突
#       -> 国别明细 -> 来源国判定 -> 只更正候选国家改变判定。
# 用法: ./scripts/demo.sh [BASE_URL]   默认 http://localhost:8000
set -euo pipefail

BASE="${1:-http://localhost:8000}"

req() { # req METHOD PATH [JSON]
  local method="$1" path="$2" body="${3:-}"
  echo
  echo "=== $method $path ${body:+-- $body}"
  if [ -n "$body" ]; then
    curl -sS -w '\nHTTP %{http_code}\n' -X "$method" "$BASE$path" \
      -H 'Content-Type: application/json' -d "$body"
  else
    curl -sS -w '\nHTTP %{http_code}\n' -X "$method" "$BASE$path"
  fi
}

echo "##### 1. 固定相邻两年底表（2023/2024，整数分）"
req PUT /base '{"amounts":[
  {"year":2023,"code":"A","amount_cents":100},
  {"year":2023,"code":"B","amount_cents":200},
  {"year":2023,"code":"C","amount_cents":300},
  {"year":2023,"code":"D","amount_cents":400},
  {"year":2024,"code":"A","amount_cents":110},
  {"year":2024,"code":"B","amount_cents":210},
  {"year":2024,"code":"C","amount_cents":310},
  {"year":2024,"code":"D","amount_cents":410}]}'

echo
echo "##### 2. 提交对照 v1：C、D 经对照相连，并为一组"
req POST /mappings '{"version":1,"entries":[
  {"old_code":"A","new_code":"A"},
  {"old_code":"B","new_code":"B"},
  {"old_code":"C","new_code":"C"},
  {"old_code":"C","new_code":"D"},
  {"old_code":"D","new_code":"D"}]}'

echo
echo "##### 3. 现行汇总（组 3 = {C,D | C,D}，各年合计 700/720）"
req GET /summary

echo
echo "##### 4. 改版 v2：把 C/D 组拆回单税号组"
req POST /mappings '{"version":2,"entries":[
  {"old_code":"A","new_code":"A"},
  {"old_code":"B","new_code":"B"},
  {"old_code":"C","new_code":"C"},
  {"old_code":"D","new_code":"D"}]}'

echo
echo "##### 5. 按版取供数变更"
req GET /changes/1
req GET /changes/2

echo
echo "##### 6. 旧版重传：原内容 -> 200 返回当时的变更"
req POST /mappings '{"version":1,"entries":[
  {"old_code":"A","new_code":"A"},
  {"old_code":"B","new_code":"B"},
  {"old_code":"C","new_code":"C"},
  {"old_code":"C","new_code":"D"},
  {"old_code":"D","new_code":"D"}]}'

echo
echo "##### 7. 旧版改内容 -> 409 冲突"
req POST /mappings '{"version":1,"entries":[
  {"old_code":"A","new_code":"A"},
  {"old_code":"B","new_code":"B"},
  {"old_code":"C","new_code":"C"},
  {"old_code":"D","new_code":"D"}]}'

echo
echo "##### 8. 对照漏掉底表税号 -> 422 整版拒收，原供数仍可查询"
req POST /mappings '{"version":3,"entries":[
  {"old_code":"A","new_code":"A"},
  {"old_code":"B","new_code":"B"},
  {"old_code":"C","new_code":"C"}]}'
req GET /summary

echo
echo "##### 9. 版本列表"
req GET /versions

echo
echo "##### 10. 国别明细：组 4 {C} 的 2023 年（已确认 CN 100/US 80 + 待核 120，合计=底表 300）"
req PUT /details/2023/C '{"confirmed":{"CN":100,"US":80},"pending":[{"amount_cents":120,"candidates":["CN","US"]}]}'

echo
echo "##### 11. 国别明细：组 4 {C} 的 2024 年（全部已确认，合计=底表 310）"
req PUT /details/2024/C '{"confirmed":{"CN":250,"US":60}}'

echo
echo "##### 12. 来源国判定 GET /groups/4/origin：2023 无法判定（两份首位不同的分摊），2024 判定 CN"
req GET /groups/4/origin

echo
echo "##### 13. 只更正候选国家：120 分待核金额的候选由 [CN,US] 改为 [US]（金额不变）"
req PUT /details/2023/C '{"confirmed":{"CN":100,"US":80},"pending":[{"amount_cents":120,"candidates":["US"]}]}'

echo
echo "##### 14. 再次判定：2023 改为 US 在所有可行分摊下独占首位"
req GET /groups/4/origin

echo
echo "##### 15. 合计不等于底表 -> 422，上次明细保留"
req PUT /details/2023/C '{"confirmed":{"CN":1}}'
req GET /details/2023/C

echo
echo "##### 16. 组 5 {D} 缺明细 -> incomplete 列出缺项"
req GET /groups/5/origin
