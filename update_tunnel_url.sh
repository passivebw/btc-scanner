#!/bin/bash
# Reads the Cloudflare tunnel URL from tunnel.log and updates the GitHub Gist.
# Called by systemd ExecStartPost after cloudflared starts.

source /root/.env 2>/dev/null

if [ -z "$GITHUB_TOKEN" ] || [ -z "$GIST_ID" ]; then
    echo "[update_tunnel_url] ERROR: GITHUB_TOKEN or GIST_ID not set in /root/.env"
    exit 1
fi

echo "[update_tunnel_url] Waiting for tunnel URL..."

for i in $(seq 1 60); do
    URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' /root/tunnel.log 2>/dev/null | tail -1)
    if [ -n "$URL" ]; then
        break
    fi
    sleep 1
done

if [ -z "$URL" ]; then
    echo "[update_tunnel_url] ERROR: No tunnel URL found after 60s"
    exit 1
fi

echo "[update_tunnel_url] Tunnel URL: $URL"

# Build JSON payload safely with Python
PAYLOAD=$(python3 -c "
import json, sys
url = sys.argv[1]
payload = json.dumps({
    'files': {
        'config.json': {
            'content': json.dumps({'api_base': url})
        }
    }
})
print(payload)
" "$URL")

HTTP=$(curl -s -o /root/gist_update.log -w "%{http_code}" \
    -X PATCH \
    -H "Authorization: token $GITHUB_TOKEN" \
    -H "Content-Type: application/json" \
    -d "$PAYLOAD" \
    "https://api.github.com/gists/$GIST_ID")

if [ "$HTTP" = "200" ]; then
    echo "[update_tunnel_url] Gist updated OK — $URL"
else
    echo "[update_tunnel_url] ERROR: Gist update returned HTTP $HTTP"
    cat /root/gist_update.log
    exit 1
fi
