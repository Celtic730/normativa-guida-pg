# Normativa per la Guida operativa P.G.

Testi **vigenti** di codici e leggi usati dalla app «Guida operativa P.G.», scaricati ogni giorno da
[Normattiva](https://www.normattiva.it) (Istituto Poligrafico e Zecca dello Stato).

- `dati/manifest.json`: elenco degli atti con la data dell'ultimo aggiornamento su Normattiva.
- `dati/<atto>.json`: articoli dell'atto (numero, rubrica, testo, note di aggiornamento).
- `aggiorna_normativa.py` + `atti.json`: programma che controlla Normattiva e riscarica **solo** gli atti cambiati.
- `.github/workflows/aggiorna.yml`: lo esegue ogni giorno.

I testi sono **informativi**: la fonte ufficiale resta la Gazzetta Ufficiale. Le parti tra doppie parentesi `(( ))` sono
modifiche successive inserite da Normattiva. Questo repository non contiene documenti riservati.
