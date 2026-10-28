"""Probe the 3 non-publicsearch foreclosure sources: what's inside (text layer? list? one notice per file?)."""
import json, os, re, subprocess, requests
H = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36'}
out = {}
def pdf_info(url, name):
    try:
        r = requests.get(url, headers=H, timeout=120); info = {'status': r.status_code, 'bytes': len(r.content), 'type': r.headers.get('content-type')}
        open(name, 'wb').write(r.content)
        pages = subprocess.run(['pdfinfo', name], capture_output=True, text=True).stdout
        info['pdfinfo'] = re.findall(r'Pages:\s*\d+', pages)
        t = subprocess.run(['pdftotext', '-layout', '-l', '3', name, '-'], capture_output=True, text=True).stdout
        info['text_chars_p1_3'] = len(t.strip()); info['text'] = t[:2500]
        return info
    except Exception as e: return {'err': str(e)[:300]}
out['fortbend_nov'] = pdf_info('https://www.fortbendcountytx.gov/sites/default/files/document-central/document-central/county-clerk-documents/home-page-county-clerk/research-foreclosures/November-2026.pdf', 'fb.pdf')
out['comal_nov'] = pdf_info('https://www.comalcounty.gov/DocumentCenter/View/8771/November-3rd-Foreclosure-Sales', 'cm.pdf')
for mon in ['November', 'December']:
    try:
        r = requests.get(f'https://apps.wilco.org/countyclerk/trustee_sales/{mon}/files.aspx', headers=H, timeout=60)
        links = re.findall(r'href="([^"]+\.pdf)"', r.text, re.I)
        out[f'wilco_{mon}'] = {'status': r.status_code, 'n_pdfs': len(links), 'first': links[:5], 'html': r.text[:1500]}
        if links:
            u = links[0] if links[0].startswith('http') else requests.compat.urljoin(r.url, links[0])
            out[f'wilco_{mon}_first_pdf'] = pdf_info(u, 'wc.pdf')
    except Exception as e: out[f'wilco_{mon}'] = {'err': str(e)[:300]}
os.makedirs('probe_out', exist_ok=True); json.dump(out, open('probe_out/pdf_sources.json', 'w'), indent=1)
