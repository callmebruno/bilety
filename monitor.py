import io
import json
import os
import smtplib
import sys
from datetime import datetime
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

RYANAIR_API = "https://www.ryanair.com/api/farfnd/v4/oneWayFares"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}


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


def fetch_price(origin: str, destination: str, date: str, currency: str) -> float | None:
    params = {
        "departureAirportIataCode": origin,
        "arrivalAirportIataCode": destination,
        "outboundDepartureDateFrom": date,
        "outboundDepartureDateTo": date,
        "currency": currency,
        "market": "pl-pl",
    }
    try:
        resp = requests.get(RYANAIR_API, params=params, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.RequestException as e:
        print(f"[ERROR] Błąd zapytania do API Ryanair: {e}", file=sys.stderr)
        return None

    fares = data.get("fares", [])
    if not fares:
        return None

    prices = [
        fare["outbound"]["price"]["value"]
        for fare in fares
        if fare.get("outbound") and fare["outbound"].get("price")
    ]
    return min(prices) if prices else None


def generate_chart(history: list, route_label: str, currency: str) -> bytes | None:
    valid = [(e["checked_at"], e["price"]) for e in history if e["price"] is not None]
    if len(valid) < 2:
        return None

    timestamps = [datetime.strptime(ts, "%Y-%m-%d %H:%M") for ts, _ in valid]
    price_vals = [p for _, p in valid]

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.plot(timestamps, price_vals, marker="o", linewidth=1.5, color="#1a73e8")
    ax.fill_between(timestamps, price_vals, alpha=0.1, color="#1a73e8")
    ax.set_title(f"Historia cen: {route_label}", fontsize=13)
    ax.set_ylabel(f"Cena ({currency})")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m %H:%M"))
    plt.xticks(rotation=30, ha="right")
    ax.grid(True, linestyle="--", alpha=0.5)
    plt.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=120)
    plt.close(fig)
    buf.seek(0)
    return buf.read()


def send_email(subject: str, body: str, cfg: dict, chart_png: bytes | None = None) -> None:
    email_from = os.environ.get("EMAIL_FROM", "")
    email_password = os.environ.get("EMAIL_PASSWORD", "")
    email_to_raw = os.environ.get("EMAIL_TO") or cfg.get("email_to", "")
    recipients = [a.strip() for a in email_to_raw.split(",") if a.strip()]

    if not email_from or not email_password or not recipients:
        print("[WARN] Brak danych email — pomijam wysyłkę.")
        return

    if chart_png:
        msg = MIMEMultipart()
        msg.attach(MIMEText(body, "plain", "utf-8"))
        img = MIMEImage(chart_png, name="wykres.png")
        img.add_header("Content-Disposition", "attachment", filename="wykres.png")
        msg.attach(img)
    else:
        msg = MIMEText(body, "plain", "utf-8")

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
    return (
        f"https://www.ryanair.com/pl/pl/booking/home/{origin}/{destination}/{date}/1/0/0/0"
    )


def check_route(route: dict, cfg: dict, prices: dict, now: str, force: bool = False) -> None:
    origin = route["origin"]
    destination = route["destination"]
    date = route["date"]
    currency = cfg.get("currency", "PLN")
    threshold = route.get("price_threshold")

    key = f"{origin}-{destination}-{date}"
    route_label = f"{origin} → {destination} ({date})"
    print(f"[{now}] Sprawdzam lot {origin} → {destination} na {date} ({currency})")

    current_price = fetch_price(origin, destination, date, currency)
    entry = prices.get(key, {"history": []})
    history = entry["history"]
    previous_price = history[-1]["price"] if history else None

    if current_price is None:
        print("[INFO] Brak dostępnych lotów / błąd API.")
        if previous_price is not None:
            subject = f"✈ Ryanair {origin}→{destination} {date}: brak lotów"
            body = (
                f"Poprzednia cena: {previous_price:.2f} {currency}\n"
                f"Aktualnie: lot niedostępny lub błąd API\n\n"
                f"Link: {ryanair_search_url(origin, destination, date)}"
            )
            send_email(subject, body, cfg)
        history.append({"price": None, "checked_at": now})
        prices[key] = {"history": history}
        return

    print(f"[INFO] Aktualna cena: {current_price:.2f} {currency} (poprzednia: {previous_price})")

    is_first_run = previous_price is None
    price_changed = not is_first_run and abs(current_price - previous_price) >= 5.0

    if not force and not is_first_run and not price_changed:
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
            threshold_line = f"\n⚠ Cena poniżej progu {threshold:.2f} {currency}!"

        future_history = history + [{"price": current_price, "checked_at": now}]
        chart_png = generate_chart(future_history, route_label, currency)

        subject = f"✈ Ryanair {origin}→{destination} {date}: {current_price:.2f} {currency}"
        body = (
            f"{change_line}{threshold_line}\n\n"
            f"Data: {date}\n"
            f"Trasa: {origin} → {destination}\n"
            f"Sprawdzono: {now}\n\n"
            f"Link: {ryanair_search_url(origin, destination, date)}"
        )
        send_email(subject, body, cfg, chart_png)

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

    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    prices = load_prices()

    for route in routes:
        check_route(route, cfg, prices, now, force=args.force)

    save_prices(prices)


if __name__ == "__main__":
    main()
