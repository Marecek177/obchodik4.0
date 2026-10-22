import hmac
import math
import os
import secrets
from decimal import Decimal, ROUND_HALF_UP

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Integer, Numeric, String, create_engine, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from starlette.middleware.sessions import SessionMiddleware


DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("Chýba nastavenie DATABASE_URL.")

# Render provides postgres://; psycopg 3 uses the postgresql+psycopg:// dialect.
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+psycopg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)

IS_PRODUCTION = os.getenv("RENDER", "") == "true"
SESSION_SECRET = os.getenv("SESSION_SECRET")
if IS_PRODUCTION and not SESSION_SECRET:
    raise RuntimeError("Na Renderi treba nastaviť SESSION_SECRET.")
if not SESSION_SECRET:
    SESSION_SECRET = "local-only-" + secrets.token_urlsafe(32)

engine = create_engine(DATABASE_URL, pool_pre_ping=True)


class Base(DeclarativeBase):
    pass


class Produkt(Base):
    __tablename__ = "produkty"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    nazov: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    cena: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    mnozstvo: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


Base.metadata.create_all(engine)

# Seed only an empty database. Existing stock is never reset during a deployment.
with Session(engine) as db:
    if db.scalar(select(Produkt.id).limit(1)) is None:
        db.add_all([
            Produkt(nazov="jablko", cena=Decimal("0.50"), mnozstvo=5),
            Produkt(nazov="mlieko", cena=Decimal("1.20"), mnozstvo=3),
            Produkt(nazov="chlieb", cena=Decimal("1.50"), mnozstvo=2),
        ])
        db.commit()

app = FastAPI(title="Obchodík")
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="lax",
    https_only=IS_PRODUCTION,
)
templates = Jinja2Templates(directory="templates")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    if IS_PRODUCTION:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response


def csrf_token(request: Request) -> str:
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


def csrf_is_valid(request: Request, token: str) -> bool:
    expected = request.session.get("csrf_token", "")
    return bool(expected) and hmac.compare_digest(expected, token)


def nacitaj_sklad() -> dict[str, dict[str, float | int]]:
    with Session(engine) as db:
        produkty = db.scalars(select(Produkt).order_by(Produkt.id)).all()
        return {
            produkt.nazov: {"cena": float(produkt.cena), "mnozstvo": produkt.mnozstvo}
            for produkt in produkty
        }


def kosik_pouzivatela(request: Request) -> list[str]:
    kosik = request.session.get("kosik", [])
    # Session cookies are client-side signed, so keep their size bounded and
    # ignore values that are not valid product-name strings.
    if not isinstance(kosik, list):
        return []
    return [item for item in kosik[:100] if isinstance(item, str) and len(item) <= 100]


def cena_kosika(kosik: list[str]) -> float:
    sklad = nacitaj_sklad()
    return round(sum(sklad[nazov]["cena"] for nazov in kosik if nazov in sklad), 2)


def obchod(request: Request, sprava: str = "", zlava: float = 0) -> HTMLResponse:
    sklad = nacitaj_sklad()
    kosik = kosik_pouzivatela(request)
    cena = cena_kosika(kosik)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "sklad": sklad,
            "kosik": kosik,
            "cena": cena,
            "zlava": zlava,
            "finalna_cena": round(cena - zlava, 2),
            "sprava": sprava,
            "csrf_token": csrf_token(request),
        },
    )


@app.get("/", response_class=HTMLResponse)
def domov(request: Request):
    return obchod(request)


@app.post("/pridat", response_class=HTMLResponse)
def pridat_do_kosika(
    request: Request,
    nazov: str = Form(..., max_length=100),
    csrf: str = Form(..., max_length=100),
):
    if not csrf_is_valid(request, csrf):
        return HTMLResponse("Neplatná požiadavka.", status_code=403)
    kosik = kosik_pouzivatela(request)
    if len(kosik) >= 100:
        return obchod(request, "Košík dosiahol maximálnu veľkosť.")

    # A single conditional UPDATE prevents two visitors from buying the last unit.
    with Session(engine) as db:
        nazov_polozky = db.scalar(
            update(Produkt)
            .where(Produkt.nazov == nazov, Produkt.mnozstvo > 0)
            .values(mnozstvo=Produkt.mnozstvo - 1)
            .returning(Produkt.nazov)
        )
        if nazov_polozky is None:
            exists = db.scalar(select(Produkt.id).where(Produkt.nazov == nazov))
            db.rollback()
            if exists is None:
                return obchod(request, "Takáto položka neexistuje.")
            return obchod(request, "Položka momentálne nie je na sklade.")
        db.commit()

    kosik.append(nazov_polozky)
    request.session["kosik"] = kosik
    return obchod(request, "Položka bola pridaná do košíka.")


@app.post("/zlava", response_class=HTMLResponse)
def pouzit_zlavu(
    request: Request,
    kod: str = Form(..., max_length=50),
    csrf: str = Form(..., max_length=100),
):
    if not csrf_is_valid(request, csrf):
        return HTMLResponse("Neplatná požiadavka.", status_code=403)
    cena = cena_kosika(kosik_pouzivatela(request))
    if hmac.compare_digest(kod.strip(), "BELI10"):
        return obchod(request, "Zľava 10 % bola použitá.", round(cena * 0.10, 2))
    return obchod(request, "Zľavový kód nie je správny.")


@app.post("/platba", response_class=HTMLResponse)
def vybrat_platbu(
    request: Request,
    sposob: str = Form(..., max_length=20),
    csrf: str = Form(..., max_length=100),
):
    if not csrf_is_valid(request, csrf):
        return HTMLResponse("Neplatná požiadavka.", status_code=403)
    if sposob == "karta":
        sprava = "Platba kartou bola vybraná."
    elif sposob == "hotovost":
        sprava = "Platba hotovosťou bola vybraná."
    else:
        sprava = "Vyber platobnú metódu."
    return obchod(request, sprava)


@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/prihlasenie", status_code=303)
    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={"sklad": nacitaj_sklad(), "sprava": "", "csrf_token": csrf_token(request)},
    )


@app.get("/admin/prihlasenie", response_class=HTMLResponse)
def prihlasovacia_stranka(request: Request):
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={"chyba": "", "csrf_token": csrf_token(request)},
    )


@app.post("/admin/prihlasenie", response_class=HTMLResponse)
def prihlasit(
    request: Request,
    username: str = Form(..., max_length=100),
    password: str = Form(..., max_length=200),
    csrf: str = Form(..., max_length=100),
):
    if not csrf_is_valid(request, csrf):
        return HTMLResponse("Neplatná požiadavka.", status_code=403)
    spravne_meno = os.getenv("ADMIN_USERNAME", "")
    spravne_heslo = os.getenv("ADMIN_PASSWORD", "")
    if (
        spravne_meno
        and spravne_heslo
        and hmac.compare_digest(username, spravne_meno)
        and hmac.compare_digest(password, spravne_heslo)
    ):
        request.session.clear()
        request.session["admin"] = True
        csrf_token(request)
        return RedirectResponse("/admin", status_code=303)

    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "chyba": "Nesprávne údaje alebo chýbajúce nastavenie správcu.",
            "csrf_token": csrf_token(request),
        },
        status_code=401,
    )


@app.post("/admin/sklad", response_class=HTMLResponse)
def ulozit_polozku(
    request: Request,
    nazov: str = Form(..., max_length=100),
    cena: float = Form(...),
    mnozstvo: int = Form(...),
    csrf: str = Form(..., max_length=100),
):
    if not request.session.get("admin"):
        return RedirectResponse("/admin/prihlasenie", status_code=303)
    if not csrf_is_valid(request, csrf):
        return HTMLResponse("Neplatná požiadavka.", status_code=403)

    nazov = nazov.strip().lower()
    if (
        not nazov
        or not math.isfinite(cena)
        or cena < 0
        or cena > 99999999
        or mnozstvo < 0
        or mnozstvo > 1000000
    ):
        sprava = "Zadaj platný názov, cenu a množstvo."
        status = 400
    else:
        cena_decimal = Decimal(str(cena)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        with Session(engine) as db:
            produkt = db.scalar(select(Produkt).where(Produkt.nazov == nazov))
            if produkt is not None:
                produkt.cena = cena_decimal
                produkt.mnozstvo += mnozstvo
                sprava = f"Sklad položky {nazov} bol doplnený."
            else:
                db.add(Produkt(nazov=nazov, cena=cena_decimal, mnozstvo=mnozstvo))
                sprava = f"Položka {nazov} bola pridaná do skladu."
            db.commit()
        status = 200

    return templates.TemplateResponse(
        request=request,
        name="admin.html",
        context={"sklad": nacitaj_sklad(), "sprava": sprava, "csrf_token": csrf_token(request)},
        status_code=status,
    )


@app.post("/admin/odhlasit")
def odhlasit(request: Request, csrf: str = Form(..., max_length=100)):
    if not csrf_is_valid(request, csrf):
        return HTMLResponse("Neplatná požiadavka.", status_code=403)
    request.session.clear()
    return RedirectResponse("/", status_code=303)
