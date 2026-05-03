#!/usr/bin/env bash
# 把所有 zone 的 Email Routing catch-all 在 otp-relay / libre-mail 之间切换。
# 需要的 token 权限：Zone → Email Routing Rules: Edit
#
# 用法:
#   bash deploy/scripts/cf_revert_catchall.sh                       # 默认切回 libre-mail
#   TARGET_WORKER=otp-relay bash deploy/scripts/cf_revert_catchall.sh
#   TARGET_WORKER=any-other-worker bash deploy/scripts/cf_revert_catchall.sh
#
# 凭证：
#   - 优先用环境变量 CF_API_TOKEN
#   - 也可放到 output/secrets.json 的 cloudflare.api_token，本脚本不读 secrets，
#     需要你手动 export CF_API_TOKEN=$(jq -r .cloudflare.api_token output/secrets.json)
#
# Zone 列表写死在脚本里，按需要改 ZONES。
set -euo pipefail

: "${CF_API_TOKEN:?Please export CF_API_TOKEN=cfut_xxx (Zone Email Routing Rules:Edit)}"
TARGET_WORKER="${TARGET_WORKER:-libre-mail}"

declare -A ZONES=(
    [zclibre.org]=f8424a07720c05f503f67909d18d8656
    [anthropices.com]=6e75cfab95c1b30af4fb42fda30ff029
    [xiaoxiaoyang.de]=e41464aad51f22196120863d74b51d86
)

for zone in "${!ZONES[@]}"; do
    zid="${ZONES[$zone]}"
    echo "=== $zone ==="
    body=$(printf '{"matchers":[{"type":"all"}],"actions":[{"type":"worker","value":["%s"]}],"enabled":true,"name":"catch-all -> %s"}' \
        "$TARGET_WORKER" "$TARGET_WORKER")
    curl -sS -X PUT \
        -H "Authorization: Bearer $CF_API_TOKEN" \
        -H "Content-Type: application/json" \
        "https://api.cloudflare.com/client/v4/zones/$zid/email/routing/rules/catch_all" \
        -d "$body" \
        | python3 -c "
import sys, json
r = json.load(sys.stdin)
if r.get('success'):
    res = r['result']
    acts = ' '.join(f\"{a['type']}->{(a.get('value') or [''])[0]}\" for a in res.get('actions',[]))
    print(f'  ✓ enabled={res.get(\"enabled\")} actions={acts}')
else:
    print(f'  ✗ {r.get(\"errors\")}')"
done

echo
echo "切换完成 → $TARGET_WORKER"
