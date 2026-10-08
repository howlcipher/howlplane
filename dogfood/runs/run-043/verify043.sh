#!/usr/bin/env bash
# Independent behavioural checks for run-043 (pingbot token support). Run from the target repo.
set -u
TOK='c2VjcmV0LXRva2VuLTQ1Njc4OQ=='   # base64 with padding, 28 chars
SHORT='ab1=x'
W=$(mktemp -d)
pass=0; fail=0
ok() { echo "PASS $1"; pass=$((pass+1)); }
bad() { echo "FAIL $1"; fail=$((fail+1)); }
check() { if eval "$2"; then ok "$1"; else bad "$1"; fi; }

# local webhook: records headers, answers with status from path
cat > "$W/server.py" <<'EOF'
import http.server, json, sys
class H(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0)); body = self.rfile.read(n)
        with open(sys.argv[2], "a") as f:
            f.write(json.dumps({"path": self.path, "auth": self.headers.get("Authorization"), "body": body.decode()}) + "\n")
        code = int(self.path.strip("/") or 200)
        self.send_response(code); self.end_headers()
    def log_message(self, *a): pass
http.server.HTTPServer(("127.0.0.1", int(sys.argv[1])), H).serve_forever()
EOF
PORT=$((20000 + RANDOM % 20000))
python3 "$W/server.py" $PORT "$W/hits.jsonl" & SRV=$!
trap 'kill $SRV 2>/dev/null; rm -rf "$W"' EXIT
sleep 0.5
U="http://127.0.0.1:$PORT"

printf 'PINGBOT_URL=%s/200\nPINGBOT_API_TOKEN = %s\n# comment\n\nPINGBOT_TIMEOUT=7\n' "$U" "$TOK" > "$W/f.conf"

# 1. token header from file, value with '=' intact
env -i PATH="$PATH" python3 -m pingbot --config "$W/f.conf" send api 1.0 > "$W/o1" 2> "$W/e1"; c=$?
check "send with file token exits 0" "[ $c = 0 ]"
check "Authorization header exact (padding kept)" "tail -1 '$W/hits.jsonl' | grep -q '\"auth\": \"Bearer $TOK\"'"
# env wins over file
env -i PATH="$PATH" PINGBOT_API_TOKEN=envtoken12345 python3 -m pingbot --config "$W/f.conf" send api 1.0 >/dev/null 2>&1
check "env token wins over file" "tail -1 '$W/hits.jsonl' | grep -q 'Bearer envtoken12345'"
# no token -> no header, payload unchanged
env -i PATH="$PATH" PINGBOT_URL="$U/200" python3 -m pingbot send api 1.0 --env staging >/dev/null 2>&1
check "no token -> no Authorization header" "tail -1 '$W/hits.jsonl' | grep -q '\"auth\": null'"
check "payload unchanged" "tail -1 '$W/hits.jsonl' | grep -qF '{\\\"channel\\\": \\\"deploys\\\", \\\"text\\\": \\\"api 1.0 deployed to staging\\\"}'"
# empty token in env -> treated as unset? (no header)
env -i PATH="$PATH" PINGBOT_URL="$U/200" PINGBOT_API_TOKEN= python3 -m pingbot send api 1.0 >/dev/null 2>&1
check "empty token -> no header" "tail -1 '$W/hits.jsonl' | grep -q '\"auth\": null'"

# 3. show-config masking
env -i PATH="$PATH" python3 -m pingbot --config "$W/f.conf" show-config > "$W/sc" 2>&1
check "show-config masks long token as ****+last4" "grep -qx 'PINGBOT_API_TOKEN=\*\*\*\*OQ==' '$W/sc'"
check "show-config keeps non-secret lines" "grep -qx 'PINGBOT_TIMEOUT=7' '$W/sc' && grep -qx 'PINGBOT_CHANNEL=deploys' '$W/sc'"
check "show-config never shows token" "! grep -qF '$TOK' '$W/sc'"
env -i PATH="$PATH" PINGBOT_API_TOKEN="$SHORT" python3 -m pingbot show-config > "$W/sc2" 2>&1
check "short token -> ****" "grep -qx 'PINGBOT_API_TOKEN=\*\*\*\*' '$W/sc2'"
env -i PATH="$PATH" python3 -m pingbot show-config > "$W/sc3" 2>&1
check "unset token not listed; output identical to before" "[ \"\$(cat '$W/sc3')\" = \$'PINGBOT_CHANNEL=deploys\nPINGBOT_TIMEOUT=10\nPINGBOT_URL=' ]"
# exactly-12 boundary
env -i PATH="$PATH" PINGBOT_API_TOKEN=abcdefgh1234 python3 -m pingbot show-config > "$W/sc4" 2>&1
check "12-char token -> ****1234" "grep -qx 'PINGBOT_API_TOKEN=\*\*\*\*1234' '$W/sc4'"
env -i PATH="$PATH" PINGBOT_API_TOKEN=abcdefg1234 python3 -m pingbot show-config > "$W/sc5" 2>&1
check "11-char token -> ****" "grep -qx 'PINGBOT_API_TOKEN=\*\*\*\*' '$W/sc5'"
# secret-named unknown key in file (e.g. DEPLOY_SECRET) is masked if shown
printf 'DEPLOY_SECRET=hunter2hunter2\nmy_api_key=zzzzzzzz9999\n' > "$W/g.conf"
env -i PATH="$PATH" python3 -m pingbot --config "$W/g.conf" show-config > "$W/sc6" 2>&1
check "file secret-named keys never shown in clear" "! grep -q 'hunter2hunter2\|zzzzzzzz9999' '$W/sc6'"

# verbose
env -i PATH="$PATH" python3 -m pingbot --config "$W/f.conf" send api 1.0 --verbose > "$W/o2" 2> "$W/e2"
check "verbose shows Bearer ****" "grep -qx 'Authorization: Bearer \*\*\*\*' '$W/e2'"
check "verbose never shows token" "! grep -qF '$TOK' '$W/e2' '$W/o2'"

# 4. 401/403 -> exit 3 exact message; 500 -> exit 1
for code in 401 403; do
  printf 'PINGBOT_URL=%s/%s\nPINGBOT_API_TOKEN=%s\n' "$U" $code "$TOK" > "$W/h.conf"
  env -i PATH="$PATH" python3 -m pingbot --config "$W/h.conf" send api 1 > "$W/o3" 2> "$W/e3"; c=$?
  check "HTTP $code -> exit 3" "[ $c = 3 ]"
  check "HTTP $code message exact" "[ \"\$(cat '$W/e3')\" = 'pingbot: authentication failed (HTTP $code): check PINGBOT_API_TOKEN' ]"
  check "HTTP $code no token in output" "! grep -qF '$TOK' '$W/e3' '$W/o3'"
done
printf 'PINGBOT_URL=%s/500\nPINGBOT_API_TOKEN=%s\n' "$U" "$TOK" > "$W/h.conf"
env -i PATH="$PATH" python3 -m pingbot --config "$W/h.conf" send api 1 > /dev/null 2> "$W/e4"; c=$?
check "HTTP 500 -> exit 1, old message" "[ $c = 1 ] && grep -qx 'pingbot: webhook returned HTTP 500' '$W/e4'"
# network error with token
env -i PATH="$PATH" PINGBOT_URL="http://127.0.0.1:1/x" PINGBOT_API_TOKEN="$TOK" python3 -m pingbot send api 1 > /dev/null 2> "$W/e5"; c=$?
check "network error exit 1, no token" "[ $c = 1 ] && ! grep -qF '$TOK' '$W/e5'"
# token in URL is not our concern; config errors
printf 'PINGBOT_URL=x\nbroken line\n' > "$W/bad.conf"
env -i PATH="$PATH" python3 -m pingbot --config "$W/bad.conf" show-config > /dev/null 2> "$W/e6"; c=$?
check "bad line -> exit 2 'expected KEY=value'" "[ $c = 2 ] && grep -q 'line 2: expected KEY=value' '$W/e6'"
# config error must not leak the token
env -i PATH="$PATH" PINGBOT_TIMEOUT=soon PINGBOT_API_TOKEN="$TOK" python3 -m pingbot show-config > "$W/o7" 2> "$W/e7"; c=$?
check "config error exit 2, no token" "[ $c = 2 ] && ! grep -qF '$TOK' '$W/e7' '$W/o7'"

echo "RESULT pass=$pass fail=$fail"
