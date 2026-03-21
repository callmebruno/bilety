import io
import json
import os
import re
import smtplib
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
load_dotenv()

WARSAW = ZoneInfo("Europe/Warsaw")
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import requests

CONFIG_FILE = Path(__file__).parent / "config.json"
PRICES_FILE = Path(__file__).parent / "prices.json"

RYANAIR_FARES_API = "https://www.ryanair.com/api/farfnd/v4/oneWayFares"
RYANAIR_AVAILABILITY_API = "https://www.ryanair.com/api/booking/v4/pl-pl/availability"
WIZZAIR_BUILDNUMBER_URL = "https://wizzair.com/buildnumber"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
HEADERS = {"User-Agent": UA, "Accept": "application/json"}

_wizzair_api_version: str | None = None


def load_config() -> dict:
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)


def load_prices() -> dict:
    if not PRICES_FILE.exists():
        return {}
    with open(PRICES_FILE, encoding="utf-8") as f:
        data = json.load(f)
    # Migracja starego formatu {"price": X, "checked_at": Y} → {"history": [...]}
    for key, val in data.items():
        if "history" not in val:
            data[key] = {"history": [{"price": val["price"], "checked_at": val["checked_at"]}]}
    return data


def save_prices(prices: dict) -> None:
    with open(PRICES_FILE, "w", encoding="utf-8") as f:
        json.dump(prices, f, indent=2, ensure_ascii=False)


def fetch_price_ryanair(origin: str, destination: str, date: str, currency: str) -> dict | None:
    """Zwraca dict {"price", "fares_left", "buckets"} lub None."""
    fare_params = {
        "departureAirportIataCode": origin,
        "arrivalAirportIataCode": destination,
        "outboundDepartureDateFrom": date,
        "outboundDepartureDateTo": date,
        "currency": currency,
        "market": "pl-pl",
    }
    try:
        resp = requests.get(RYANAIR_FARES_API, params=fare_params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[ERROR] Błąd zapytania do API cen: {e}", file=sys.stderr)
        return None

    fares = data.get("fares", [])
    if not fares:
        return None

    prices = [
        fare["outbound"]["price"]["value"]
        for fare in fares
        if fare.get("outbound") and fare["outbound"].get("price")
    ]
    if not prices:
        return None
    price = min(prices)

    # --- dostępność miejsc + koszyki cenowe (availability API) ---
    fares_left = -1
    buckets = []
    try:
        avail_prices = {}
        for adt in range(1, 26):
            avail_params = {
                "ADT": adt, "CHD": 0, "TEEN": 0, "INF": 0,
                "DateOut": date,
                "Origin": origin,
                "Destination": destination,
                "Disc": 0,
                "promoCode": "",
                "IncludeConnectingFlights": "false",
                "ToUs": "AGREED",
            }
            resp2 = requests.get(RYANAIR_AVAILABILITY_API, params=avail_params, headers=HEADERS, timeout=30)
            resp2.raise_for_status()
            data2 = resp2.json()
            for trip in data2.get("trips", []):
                for dt in trip.get("dates", []):
                    for flight in dt.get("flights", []):
                        if adt == 1:
                            fl = flight.get("faresLeft", -1)
                            if fl >= 0 and (fares_left < 0 or fl < fares_left):
                                fares_left = fl
                        rf = flight.get("regularFare")
                        if rf:
                            for f in rf.get("fares", []):
                                avail_prices[adt] = f["amount"]

        # Przelicz koszyki na walutę docelową (PLN)
        # availability API zwraca walutę kraju wylotu (np. EUR, MAD)
        # fares API zwraca walutę z configu (PLN) — używamy proporcji
        if avail_prices and avail_prices.get(1):
            ratio = price / avail_prices[1]  # np. 310 PLN / 91 EUR = 3.41

            prev_raw = None
            bucket_start = 1
            for adt in range(1, 26):
                ap = avail_prices.get(adt)
                if ap is None:
                    continue
                if prev_raw is not None and ap != prev_raw:
                    buckets.append({"price": round(prev_raw * ratio, 2), "seats": adt - bucket_start})
                    bucket_start = adt
                prev_raw = ap
            if prev_raw is not None:
                buckets.append({"price": round(prev_raw * ratio, 2), "seats_min": 26 - bucket_start, "is_last": True})
    except requests.RequestException:
        pass  # dostępność opcjonalna

    return {"price": price, "fares_left": fares_left, "buckets": buckets}


# ── WizzAir ──────────────────────────────────────────────────────────────────

def get_wizzair_api_version() -> str | None:
    global _wizzair_api_version
    if _wizzair_api_version:
        return _wizzair_api_version
    try:
        resp = requests.get(WIZZAIR_BUILDNUMBER_URL, headers={"User-Agent": UA}, timeout=15)
        resp.raise_for_status()
        match = re.search(r"https://be\.wizzair\.com/([0-9.]+)", resp.text)
        if match:
            _wizzair_api_version = match.group(1)
            print(f"[INFO] WizzAir API version: {_wizzair_api_version}")
            return _wizzair_api_version
    except requests.RequestException as e:
        print(f"[ERROR] WizzAir API version: {e}", file=sys.stderr)
    return None


def fetch_price_wizzair(origin: str, destination: str, date: str, currency: str) -> dict | None:
    """Zwraca dict {"price", "fares_left", "buckets"} lub None."""
    version = get_wizzair_api_version()
    if not version:
        return None

    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://wizzair.com",
        "Referer": "https://wizzair.com/pl-pl/flights/timetable",
        "Content-Type": "application/json;charset=UTF-8",
    })

    url = f"https://be.wizzair.com/{version}/Api/search/timetable"
    payload = {
        "flightList": [{
            "departureStation": origin,
            "arrivalStation": destination,
            "from": date,
            "to": date,
        }],
        "priceType": "regular",
        "adultCount": 1,
        "childCount": 0,
        "infantCount": 0,
    }

    try:
        resp = session.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[ERROR] WizzAir API: {e}", file=sys.stderr)
        return None

    flights = data.get("outboundFlights", [])
    if not flights:
        return None

    best_price = None
    for fl in flights:
        amount = fl.get("price", {}).get("amount")
        if amount is not None and (best_price is None or amount < best_price):
            best_price = amount

    if best_price is None:
        return None

    return {"price": best_price, "fares_left": -1, "buckets": []}


# ── Wspólne ──────────────────────────────────────────────────────────────────

def generate_chart(history: list, route_label: str, currency: str) -> bytes | None:
    valid = [(e["checked_at"], e["price"]) for e in history if e["price"] is not None]
    if len(valid) < 2:
        return None

    timestamps = [datetime.strptime(ts, "%Y-%m-%d %H:%M") for ts, _ in valid]
    price_vals = [p for _, p in valid]

    fig, ax = plt.subplots(figsize=(10, 3))
    ax.plot(timestamps, price_vals, marker="o", linewidth=1.5, color="#1a73e8")
    ax.fill_between(timestamps, price_vals, alpha=0.1, color="#1a73e8")
    ax.set_title(f"Historia cen: {route_label}", fontsize=13)
    ax.set_ylabel(f"Cena ({currency})")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m %H:%M"))
    plt.xticks(rotation=30, ha="right")
    min_p, max_p = min(price_vals), max(price_vals)
    margin = max((max_p - min_p) * 0.15, 5)
    ax.set_ylim(min_p - margin, max_p + margin)
    ax.grid(True, linestyle="--", alpha=0.5)
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def build_email_html(
    origin: str,
    destination: str,
    change_line: str,
    threshold_line: str,
    buckets: list,
    currency: str,
    date: str,
    now: str,
    link: str,
    has_chart: bool,
) -> str:
    S = "border-collapse:collapse;width:100%;font-size:13px"
    TH = "padding:5px 10px;text-align:center;color:white;background:#0d47a1"
    TD = "padding:4px 10px;border-bottom:1px solid #e8e8e8"

    # Tabela koszyków — pasek poziomy z kolorami
    buckets_html = ""
    if buckets:
        total_seats = 0
        cols_header = ""
        cols_seats = ""
        colors = ["#0d47a1", "#1565c0", "#1976d2", "#1e88e5", "#42a5f5", "#64b5f6", "#90caf9"]
        for i, b in enumerate(buckets):
            if b.get("is_last"):
                seats_str = f"{b['seats_min']}+"
                total_seats += b["seats_min"]
            else:
                seats_str = str(b["seats"])
                total_seats += b["seats"]
            bg = colors[i % len(colors)]
            cols_header += f'<td style="padding:6px 4px;text-align:center;color:white;background:{bg};font-size:12px;white-space:nowrap"><b>{b["price"]:.0f}</b> {currency}</td>'
            cols_seats += f'<td style="padding:4px;text-align:center;font-size:12px;background:#f5f8ff">{seats_str}</td>'

        buckets_html = f"""
        <table style="{S};margin:12px 0;border:1px solid #ccc;border-radius:4px;overflow:hidden">
          <tr>{cols_header}</tr>
          <tr>{cols_seats}</tr>
          <tr><td colspan="{len(buckets)}" style="padding:4px;text-align:center;font-size:11px;color:#666;background:#f0f0f0">Dostępnych miejsc: min. <b>{total_seats}</b></td></tr>
        </table>"""

    threshold_html = ""
    if threshold_line:
        threshold_html = f'<div style="background:#fff3e0;border-left:3px solid #ff9800;padding:6px 10px;margin:8px 0;font-size:13px;color:#e65100">{threshold_line}</div>'

    chart_html = ""
    if has_chart:
        chart_html = '<img src="cid:chart" style="width:100%;border-radius:4px;margin:8px 0">'

    return f"""<html>
<body style="font-family:-apple-system,Arial,sans-serif;color:#222;margin:0;padding:0">
  <table width="100%" cellpadding="0" cellspacing="0" style="max-width:600px">
    <tr><td style="background:#0d47a1;padding:12px 16px">
      <!--[if mso]><table width="100%"><tr><td><![endif]-->
      <a href="{link}" style="float:right;color:white;background:rgba(255,255,255,0.2);padding:5px 12px;border-radius:4px;text-decoration:none;font-size:12px;font-weight:bold;margin-left:12px;margin-top:4px">Rezerwuj &rarr;</a>
      <span style="color:white;font-size:18px;font-weight:bold">✈ {origin} &rarr; {destination}</span><br><span style="color:rgba(255,255,255,0.7);font-size:12px">{date}</span>
      <!--[if mso]></td></tr></table><![endif]-->
    </td></tr>
    <tr><td style="padding:14px 16px">
      <p style="font-size:15px;margin:0 0 4px;color:#333">{change_line}</p>
      {threshold_html}
      {buckets_html}
      {chart_html}
      <p style="font-size:11px;color:#aaa;margin:10px 0 0">Sprawdzono: {now}</p>
    </td></tr>
  </table>
</body>
</html>"""


def send_email(subject: str, body: str, email_to_raw: str, chart_png: bytes | None = None) -> None:
    email_from = os.environ.get("EMAIL_FROM", "")
    email_password = os.environ.get("EMAIL_PASSWORD", "")
    recipients = [a.strip() for a in email_to_raw.split(",") if a.strip()]

    if not email_from or not email_password or not recipients:
        print("[WARN] Brak danych email — pomijam wysyłkę.")
        return

    if chart_png:
        msg = MIMEMultipart("related")
        msg.attach(MIMEText(body, "html", "utf-8"))
        img = MIMEImage(chart_png)
        img.add_header("Content-ID", "<chart>")
        img.add_header("Content-Disposition", "inline", filename="wykres.png")
        msg.attach(img)
    else:
        msg = MIMEMultipart("related")
        msg.attach(MIMEText(body, "html", "utf-8"))

    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = ", ".join(recipients)

    try:
        with smtplib.SMTP_SSL("poczta.interia.pl", 465) as smtp:
            smtp.login(email_from, email_password)
            smtp.sendmail(email_from, recipients, msg.as_string())
        print(f"[OK] Email wysłany do {', '.join(recipients)}")
    except Exception as e:
        print(f"[ERROR] Nie udało się wysłać emaila: {e}", file=sys.stderr)


def ryanair_search_url(origin: str, destination: str, date: str) -> str:
    return f"https://www.ryanair.com/pl/pl/booking/home/{origin}/{destination}/{date}/1/0/0/0"


def wizzair_search_url(origin: str, destination: str, date: str) -> str:
    return f"https://wizzair.com/pl-pl/booking/select-flight/{origin}/{destination}/{date}/null/1/0/0/null"


def check_route(route: dict, cfg: dict, prices: dict, now: str, force: bool = False) -> None:
    origin = route["origin"]
    destination = route["destination"]
    date = route["date"]
    currency = cfg.get("currency", "PLN")
    threshold = route.get("price_threshold")
    airline = route.get("airline", "ryanair").lower()

    global_email = os.environ.get("EMAIL_TO") or cfg.get("email_to", "")
    email_to = route.get("email_to") or global_email

    key = f"{origin}-{destination}-{date}"
    airline_label = "WizzAir" if airline == "wizzair" else "Ryanair"
    print(f"[{now}] [{airline_label}] Sprawdzam {origin} → {destination} na {date} ({currency})")

    if airline == "wizzair":
        result = fetch_price_wizzair(origin, destination, date, currency)
        search_url = wizzair_search_url(origin, destination, date)
    else:
        result = fetch_price_ryanair(origin, destination, date, currency)
        search_url = ryanair_search_url(origin, destination, date)

    entry = prices.get(key, {"history": []})
    history = entry["history"]
    previous_price = history[-1]["price"] if history else None

    if result is None:
        print("[INFO] Brak dostępnych lotów / błąd API.")
        if previous_price is not None:
            subject = f"[{airline_label}] {origin}→{destination} {date}: brak lotów"
            body = f"""<html>
<body style="font-family:Arial,sans-serif;color:#333;max-width:600px;margin:0;padding:16px">
  <h2 style="color:#d32f2f">✈ {origin} → {destination} ({date})</h2>
  <p>Poprzednia cena: <b>{previous_price:.2f} {currency}</b></p>
  <p style="color:#d32f2f;font-weight:bold">Lot niedostępny lub błąd API</p>
  <p><a href="{search_url}" style="display:inline-block;background:#1a73e8;color:white;padding:8px 16px;border-radius:4px;text-decoration:none">Sprawdź →</a></p>
</body></html>"""
            send_email(subject, body, email_to)
        history.append({"price": None, "checked_at": now})
        prices[key] = {"history": history}
        return

    current_price = result["price"]
    fares_left = result["fares_left"]
    buckets = result.get("buckets", [])

    if fares_left >= 0:
        print(f"[INFO] Cena: {current_price:.2f} {currency} (poprz.: {previous_price}) — zostało {fares_left} miejsc")
    else:
        print(f"[INFO] Cena: {current_price:.2f} {currency} (poprz.: {previous_price})")
    if buckets:
        print(f"[INFO] Koszyki cenowe: {len(buckets)} poziomów")

    total_seats = 0
    for b in buckets:
        total_seats += b.get("seats", 0) + b.get("seats_min", 0)
    low_availability = buckets and total_seats < 25

    is_first_run = previous_price is None
    price_changed = not is_first_run and abs(current_price - previous_price) >= 5.0

    if not force and not is_first_run and not price_changed and not low_availability:
        print("[INFO] Cena bez zmian, email nie wysłany.")
    elif not force and threshold is not None and not is_first_run and current_price > threshold:
        print(f"[INFO] Cena zmieniła się, ale {current_price:.2f} > próg {threshold:.2f} — email pominięty.")
    else:
        if is_first_run:
            change_line = f"Pierwsza zarejestrowana cena: {current_price:.2f} {currency}"
        else:
            diff = current_price - previous_price
            change_line = f"Zmiana: {previous_price:.2f} → {current_price:.2f} {currency} ({diff:+.2f} {currency})"

        threshold_line = ""
        if threshold is not None and current_price <= threshold:
            threshold_line = f"⚠ Cena poniżej progu {threshold:.2f} {currency}!"
        if low_availability:
            low_msg = f"⚠ Mało miejsc! Dostępnych: {total_seats}"
            threshold_line = f"{threshold_line}<br>{low_msg}" if threshold_line else low_msg

        future_history = history + [{"price": current_price, "checked_at": now}]
        chart_png = generate_chart(future_history, f"{origin} → {destination}", currency)

        subject = f"[{airline_label}] {origin}→{destination} {date}: {current_price:.2f} {currency}"
        body_html = build_email_html(
            origin=origin,
            destination=destination,
            change_line=change_line,
            threshold_line=threshold_line,
            buckets=buckets,
            currency=currency,
            date=date,
            now=now,
            link=search_url,
            has_chart=chart_png is not None,
        )
        send_email(subject, body_html, email_to, chart_png)

    history.append({"price": current_price, "checked_at": now})
    prices[key] = {"history": history}


def main() -> None:
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Wyślij email niezależnie od zmiany ceny")
    args = parser.parse_args()

    cfg = load_config()
    routes = cfg.get("routes", [])
    if not routes:
        print("[ERROR] Brak tras w config.json (klucz 'routes').", file=sys.stderr)
        sys.exit(1)

    now = datetime.now(WARSAW).strftime("%Y-%m-%d %H:%M")
    prices = load_prices()

    for route in routes:
        check_route(route, cfg, prices, now, force=args.force)

    save_prices(prices)


if __name__ == "__main__":
    main()
