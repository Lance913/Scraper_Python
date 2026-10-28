"""Probe 2: OCR samples of Fort Bend / Comal / Williamson notices + listing pages."""
import json, os, re, subprocess, requests
H = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'}
out = {}
def get(u, name):
    r = requests.get(u, headers=H, timeout=180); open(name, 'wb').write(r.content); return r
def ocr(name, first, last):
    res = {}
    for p in range(first, last + 1):
        subprocess.run(['pdftoppm', '-r', '200', '-f', str(p), '-l', str(p), '-png', name, 'pg'], check=False)
        imgs = sorted(f for f in os.listdir('.') if f.startswith('pg') and f.endswith('.png'))
        if not imgs: continue
        t = subprocess.run(['tesseract', imgs[-1], '-'], capture_output=True, text=True).stdout
        res[p] = t[:1800]
        for f in imgs: os.remove(f)
    return res
try:
    get('https://www.fortbendcountytx.gov/sites/default/files/document-central/document-central/county-clerk-documents/home-page-county-clerk/research-foreclosures/November-2026.pdf', 'fb.pdf')
    out['fortbend_ocr'] = ocr('fb.pdf', 3, 9)
except Exception as e: out['fortbend_err'] = str(e)
try:
    r = requests.get('https://www.comalcounty.gov/213/Foreclosure-Sales', headers=H, timeout=60)
    out['comal_links'] = sorted(set(re.findall(r'href="([^"]*DocumentCenter[^"]*)"', r.text)))
    get('https://www.comalcounty.gov/DocumentCenter/View/8771/November-3rd-Foreclosure-Sales', 'cm.pdf')
    out['comal_ocr'] = ocr('cm.pdf', 1, 6)
except Exception as e: out['comal_err'] = str(e)
try:
    r = requests.get('https://apps.wilco.org/countyclerk/trustee_sales/', headers=H, timeout=60)
    out['wilco_root'] = {'status': r.status_code, 'links': re.findall(r'href="([^"]+)"', r.text)[:60]}
except Exception as e: out['wilco_root_err'] = str(e)
for mon in ['November', 'December', 'January']:
    try:
        r = requests.get(f'https://apps.wilco.org/countyclerk/trustee_sales/{mon}/files.aspx', headers=H, timeout=60)
        links = re.findall(r'HREF="([^"]+\.pdf)"', r.text, re.I)
        idx = [l for l in links if 'IDX' in l.upper()]
        out[f'wilco_{mon}'] = {'status': r.status_code, 'n': len(links), 'idx': idx, 'last5': links[-5:]}
        base = f'https://apps.wilco.org/countyclerk/trustee_sales/{mon}/'
        if idx:
            get(base + idx[-1], 'idx.pdf')
            out[f'wilco_{mon}_idx_text'] = subprocess.run(['pdftotext', '-layout', 'idx.pdf', '-'], capture_output=True, text=True).stdout[:9000]
        if mon == 'December' and links:
            get(base + links[0], 'n.pdf'); out['wilco_dec_notice_ocr'] = ocr('n.pdf', 1, 2)
    except Exception as e: out[f'wilco_{mon}_err'] = str(e)
os.makedirs('probe_out', exist_ok=True); json.dump(out, open('probe_out/pdf_sources2.json', 'w'), indent=1)
