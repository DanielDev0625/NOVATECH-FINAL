import json, random, sys, time, urllib.request, urllib.error
from datetime import date

if len(sys.argv) < 3:
    sys.exit("Uso: python simulador_medidor.py URL CLAVE [pico]")
URL, CLAVE = sys.argv[1].rstrip("/"), sys.argv[2]
PICO = len(sys.argv) > 3 and sys.argv[3] == "pico"

while True:
    cantidad = 300 if PICO else round(random.uniform(90, 110), 2)
    cuerpo = {"lecturas": [{"id_externo": f"sim-{int(time.time() * 1000)}", "fuente_nombre": "Compresor 1",
                            "fecha": date.today().isoformat(), "cantidad": cantidad}]}
    req = urllib.request.Request(URL + "/api/ingesta/energia", data=json.dumps(cuerpo).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-API-Key": CLAVE})
    try:
        print(cantidad, "kWh →", urllib.request.urlopen(req, timeout=15).read().decode())
    except urllib.error.HTTPError as e:
        print("Error", e.code, e.read().decode())
    if PICO:
        break
    time.sleep(5)