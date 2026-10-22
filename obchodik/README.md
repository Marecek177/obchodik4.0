# Obchodík s PostgreSQL

FastAPI obchod ukladá sklad do PostgreSQL. Zmeny produktov a množstva prežijú reštart aplikácie aj nové nasadenie kódu, pretože databáza je samostatná služba Renderu.

## Lokálne spustenie

Vyžaduje Python 3.11+ a lokálny PostgreSQL. V PowerShelli:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:DATABASE_URL = "postgresql://uzivatel:heslo@localhost:5432/obchodik"
$env:ADMIN_USERNAME = "admin"
$env:ADMIN_PASSWORD = "zvol-si-silne-heslo"
$env:SESSION_SECRET = "nastav-dlhý-náhodný-reťazec"
uvicorn main:app --reload
```

Aplikácia beží na `http://127.0.0.1:8000`; administrácia je na `/admin`. Tabuľka sa vytvorí pri štarte. Východiskové produkty sa vložia iba vtedy, keď je databáza prázdna.

## Nasadenie na Render

1. Nahraj obsah priečinka `obchodik` do koreňového priečinka GitHub repozitára.
2. V Renderi vytvor Blueprint z tohto repozitára. Render prečíta `render.yaml` a pripraví webovú službu aj PostgreSQL databázu.
3. Pri prvom vytvorení doplň `ADMIN_USERNAME` a `ADMIN_PASSWORD`; `SESSION_SECRET` a `DATABASE_URL` sa nastavia cez Blueprint.
4. Po nasadení otvor adresu aplikácie a skontroluj obchod aj `/admin`.

Blueprint používa bezplatný plán `free` pre webovú službu a platený plán `basic-256mb` pre PostgreSQL databázu, ktorá uchováva dáta trvalo. Ak už máš v Renderi databázu, môžeš ju použiť namiesto vytvorenia novej: v nastavení služby nastav `DATABASE_URL` na jej interný connection string a odstráň databázu z `render.yaml` pred synchronizáciou Blueprintu. Nikdy nevkladaj heslo databázy ani `SESSION_SECRET` do GitHubu.

## Základné ochrany

- SQLAlchemy používa parametrizované SQL výrazy; vstup produktu sa nepripája do SQL reťazcov.
- Jinja2 automaticky escapuje hodnoty vložené do HTML šablón.
- Formuláre POST vyžadujú CSRF token a session cookie je podpísaná, s `SameSite=Lax`; na Renderi sa posiela iba cez HTTPS.
- Aplikácia posiela CSP a hlavičky proti vloženiu do rámca a nesprávnemu MIME typu.
- Zásoba sa znižuje jedným podmieneným SQL príkazom, takže súbežné požiadavky nepredajú posledný kus dvakrát.

Toto sú základné opatrenia pre školský projekt. Ak sa obchod rozšíri o účty zákazníkov alebo skutočné platby, treba doplniť plnohodnotnú autentifikáciu, limity pokusov a platobnú bránu.

