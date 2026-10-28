"""Probe 3: Montgomery County clerk foreclosure page."""
import json, os, re, requests
H = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'}
out = {}
for u in ['https://www.mctx.org/index_clerk/public_records/foreclosures/index.php', 'https://www.mctx.org/public_records/foreclosures/index.php']:
    try:
        r = requests.get(u, headers=H, timeout=60)
        links = re.findall(r'href="([^"]+)"[^>]*>([^<]{0,80})', r.text)
        out[u] = {'status': r.status_code, 'links': [l for l in links if re.search(r'forecl|trustee|notice|\.pdf|sale', l[0] + l[1], re.I)][:80],
                  'text': re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', r.text))[:3000]}
    except Exception as e: out[u] = str(e)
os.makedirs('probe_out', exist_ok=True); json.dump(out, open('probe_out/montgomery.json', 'w'), indent=1)
