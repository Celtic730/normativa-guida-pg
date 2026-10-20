"""Ripara i numeri d'articolo scritti «icolo N» (errore del vecchio scaricatore: da «Articolo 181» toglieva «art» e lasciava «icolo 181»).
Uso: python ripara_numeri.py     (corregge dati/*.json e ricalcola l'hash come fa aggiorna_normativa.py)"""
import glob, hashlib, io, json, re, sys

n_atti = n_art = 0
for f in sorted(glob.glob("dati/*.json")):
    if f.endswith("manifest.json"):
        continue
    try:
        d = json.loads(io.open(f, encoding="utf-8").read())
    except ValueError:
        continue
    if not isinstance(d, dict) or not d.get("articoli"):
        continue
    ch = 0
    for x in d["articoli"]:
        m = re.match(r"^icolo\s*(.*)$", x["n"], re.I)
        if m:
            x["n"] = m.group(1).strip(" .")
            ch += 1
    if ch:
        d["hash"] = hashlib.sha256(json.dumps(d["articoli"], ensure_ascii=False).encode()).hexdigest()[:16]
        io.open(f, "w", encoding="utf-8", newline="").write(json.dumps(d, ensure_ascii=False, separators=(",", ":")))
        n_atti += 1
        n_art += ch
print("atti riparati:", n_atti, "| articoli corretti:", n_art)
