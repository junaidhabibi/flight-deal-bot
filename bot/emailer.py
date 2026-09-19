"""Email delivery: plain-text plus a readable HTML version.

The emails are built to be actionable on a phone at 6am: price and route in
the subject line, the booking link right under each deal, and the caveats
(separate tickets, fare may be gone) stated plainly rather than buried.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate
from typing import Any, Dict, List, Optional, Sequence

from .models import Deal
from .sources.rss_deals import FeedItem

log = logging.getLogger(__name__)

TIER_LABEL = {
    "insane": "PROBABLE ERROR FARE",
    "great": "EXCEPTIONAL",
    "good": "STRONG DEAL",
    "watch": "WORTH WATCHING",
}

TIER_COLOR = {
    "insane": "#b91c1c",
    "great": "#c2410c",
    "good": "#047857",
    "watch": "#475569",
}


class EmailError(Exception):
    pass


def build_ssl_context() -> ssl.SSLContext:
    """A TLS context that works on a stock macOS Python install.

    Python downloaded from python.org ships its own OpenSSL and does NOT
    read the macOS system keychain. Until you run its bundled
    "Install Certificates.command", its trust store is empty, so every TLS
    handshake dies with CERTIFICATE_VERIFY_FAILED -- including the one to
    smtp.gmail.com. Linux (and so GitHub Actions) is unaffected.

    Rather than make that a manual setup step people hit at the worst
    moment, fall back to certifi's CA bundle, which ships with requests and
    is therefore already installed. This does NOT weaken anything:
    certificates are still fully verified, just against a bundle that
    actually has CAs in it instead of one that is empty.
    """
    context = ssl.create_default_context()
    try:
        has_cas = context.cert_store_stats().get("x509_ca", 0) > 0
    except Exception:
        has_cas = True  # can't tell; assume the platform knows best

    if has_cas:
        return context

    try:
        import certifi
    except ImportError:
        log.warning(
            "System trust store is empty and certifi isn't installed. "
            "TLS will fail. Run: pip3 install certifi"
        )
        return context

    log.info("System trust store is empty -- verifying against certifi instead.")
    return ssl.create_default_context(cafile=certifi.where())


class Emailer:
    def __init__(
        self,
        smtp_host: str,
        smtp_port: int,
        username: str,
        password: str,
        to_address: str,
        from_name: str = "Flight Deal Bot",
        dry_run: bool = False,
    ):
        self.host = smtp_host
        self.port = int(smtp_port)
        self.username = username
        self.password = password
        self.to_address = to_address
        self.from_name = from_name
        self.dry_run = dry_run
        self.sent_count = 0
        self.last_message: Optional[EmailMessage] = None

    # ---------- transport ----------

    def send(self, subject: str, text_body: str, html_body: str = "") -> bool:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = formataddr((self.from_name, self.username))
        msg["To"] = self.to_address
        msg["Date"] = formatdate(localtime=True)
        msg.set_content(text_body)
        if html_body:
            msg.add_alternative(html_body, subtype="html")

        self.last_message = msg

        if self.dry_run:
            log.info("[dry-run] would send: %s", subject)
            print("\n" + "=" * 70)
            print(f"SUBJECT: {subject}")
            print("=" * 70)
            print(text_body)
            print("=" * 70 + "\n")
            self.sent_count += 1
            return True

        try:
            context = build_ssl_context()
            with smtplib.SMTP(self.host, self.port, timeout=30) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(self.username, self.password)
                server.send_message(msg)
            self.sent_count += 1
            log.info("Sent: %s", subject)
            return True
        except smtplib.SMTPAuthenticationError as e:
            raise EmailError(
                "SMTP login rejected. For Gmail you need an App Password "
                "(not your normal password), with 2FA enabled on the account. "
                "Check SMTP_USER and SMTP_PASS are set correctly -- "
                "`echo $SMTP_USER` should show just your address, with no "
                "stray comma or quote on the end."
            ) from e
        except ssl.SSLCertVerificationError as e:
            raise EmailError(
                f"TLS certificate verification failed: {e}\n\n"
                "This is almost always a macOS Python install whose trust "
                "store was never set up. Fix it once with:\n"
                "    open /Applications/Python*/Install\\ Certificates.command\n"
                "or install certifi (`pip3 install certifi`), which this bot "
                "will then use automatically. GitHub Actions is unaffected."
            ) from e
        except Exception as e:
            raise EmailError(f"Failed to send email: {e}") from e

    # ---------- composition ----------

    def send_deals(
        self,
        deals: Sequence[Deal],
        urgent: bool = False,
        rss_items: Sequence[FeedItem] = (),
        max_deals: int = 12,
    ) -> bool:
        if not deals and not rss_items:
            return False

        deals = list(deals)[:max_deals]
        subject = self._subject(deals, urgent, rss_items)
        text = self._text_body(deals, rss_items, urgent)
        html = self._html_body(deals, rss_items, urgent)
        return self.send(subject, text, html)

    def _subject(
        self, deals: Sequence[Deal], urgent: bool, rss_items: Sequence[FeedItem]
    ) -> str:
        if not deals:
            n = len(rss_items)
            return f"[Flights] {n} error-fare post{'s' if n != 1 else ''} worth a look"

        best = deals[0]
        prefix = "[!]" if urgent else "[Flights]"
        if best.tier == "insane":
            prefix = "[!!! ERROR FARE]"

        via = f" via {best.stopover_city}" if best.stopover_city else ""
        core = (
            f"${best.total_price_usd:,.0f} {best.origin}->"
            f"{best.destination_city}{via} ({best.discount_pct:.0f}% off)"
        )
        if len(deals) > 1:
            core += f" +{len(deals) - 1} more"
        return f"{prefix} {core}"

    # ---------- plain text ----------

    def _text_body(
        self, deals: Sequence[Deal], rss_items: Sequence[FeedItem], urgent: bool
    ) -> str:
        lines: List[str] = []
        now = datetime.now().strftime("%a %b %d, %I:%M %p")
        lines.append("FLIGHT DEAL BOT")
        lines.append(now)
        lines.append("")

        if urgent:
            lines.append("These cleared the instant-alert bar. Fares this low")
            lines.append("usually last hours, not days.")
            lines.append("")

        for i, d in enumerate(deals, 1):
            label = TIER_LABEL.get(d.tier, d.tier.upper())
            lines.append("-" * 62)
            lines.append(f"{i}. {label} -- ${d.total_price_usd:,.0f} all-in")
            lines.append("-" * 62)
            if d.bag_fee_usd:
                lines.append(
                    f"   Price:      ${d.price_usd:,.0f} fare "
                    f"+ ${d.bag_fee_usd:,.0f} carry-on "
                    f"({d.ticket_count} ticket{'s' if d.ticket_count > 1 else ''})"
                )
            lines.append(f"   Route:      {d.origin} -> {d.destination_city} ({d.destination})")
            if d.stopover_city:
                days = (d.stopover_hours or 0) / 24
                lines.append(
                    f"   Stopover:   {d.stopover_city} ({d.stopover_code}) "
                    f"-- {days:.1f} days"
                )
            elif d.layover_hours is not None and d.layover_hours > 0:
                approx = "~" if d.layover_estimated else ""
                lines.append(
                    f"   Layover:    {approx}{d.layover_hours:.1f}h"
                    + (" (estimated)" if d.layover_estimated else "")
                )
            elif d.layover_band == "quick" and d.layover_hours == 0:
                # Only call it nonstop if the itinerary actually has no
                # stops. The estimator clamps a negative excess to 0.0, and
                # its expected flight time is deliberately generous, so a
                # 1-stop fare landing on 0.0 hours is routine -- this line
                # used to tell the reader a connecting fare was a nonstop,
                # which is the single fact most likely to make them book
                # without checking.
                stops = d.legs[0].stops if d.legs else None
                if stops == 0:
                    lines.append("   Layover:    none -- nonstop")
                else:
                    lines.append(
                        "   Layover:    connection, length not reported "
                        "-- CHECK BEFORE BOOKING"
                    )
            lines.append(f"   Depart:     {d.depart_date.strftime('%a %b %d, %Y')}")
            if d.return_date:
                lines.append(
                    f"   Return:     {d.return_date.strftime('%a %b %d, %Y')} "
                    f"({d.nights} nights)"
                )
            if d.airline:
                lines.append(f"   Airline:    {d.airline}")

            lines.append("")
            if d.typical_price:
                lines.append("   " + "." * 52)
                lines.append(
                    f"   Normally:   ${d.typical_price:,.0f}"
                    + (
                        f"   (Google range ${d.typical_low:,.0f}-"
                        f"${d.typical_high:,.0f})"
                        if d.typical_low and d.typical_high
                        else ""
                    )
                )
                lines.append(f"   You pay:    ${d.total_price_usd:,.0f}")
                if d.savings_usd and d.savings_usd > 0:
                    lines.append(
                        f"   You save:   ${d.savings_usd:,.0f} "
                        f"({d.savings_pct:.0f}% off normal)"
                    )
                lines.append(f"   Based on:   {d.typical_basis}")
                lines.append("   " + "." * 52)
                lines.append("")

            lines.append(
                f"   Discount:   {d.discount_pct:.0f}% below "
                f"${d.reference_price:,.0f} (the alert threshold)"
            )
            lines.append(f"   Benchmark:  {self._basis_label(d)}")
            if d.is_record:
                if d.previous_record:
                    lines.append(
                        f"   RECORD:     Beats previous low of ${d.previous_record:,.0f}"
                    )
                else:
                    lines.append("   RECORD:     Lowest seen for this route")
            if d.verified_by:
                lines.append(f"   Verified:   cross-checked against Google Flights")

            if d.legs and d.stopover_code:
                lines.append("")
                lines.append("   Itinerary:")
                for leg in d.legs:
                    when = leg.depart_at.strftime("%b %d %H:%M")
                    price = f" ${leg.price_usd:,.0f}" if leg.price_usd else ""
                    lines.append(
                        f"     {leg.origin} -> {leg.destination}  {when}"
                        f"  {leg.airline}{leg.flight_number}{price}"
                    )

            if d.bag_warnings:
                lines.append("")
                header = {
                    "oversize": "   CARRY-ON -- LIKELY A PROBLEM HERE:",
                    "tight": "   CARRY-ON -- POSSIBLE PROBLEM:",
                    "low": "   CARRY-ON -- LOW RISK:",
                }.get(d.bag_risk, "   CARRY-ON NOTES:")
                lines.append(header)
                for w in d.bag_warnings:
                    lines.append(f"     - {w}")

            if d.notes:
                lines.append("")
                for note in d.notes:
                    lines.append(f"   * {note}")

            if d.booking_url:
                lines.append("")
                lines.append(f"   BOOK: {d.booking_url}")
            lines.append("")

        if rss_items:
            lines.append("")
            lines.append("=" * 62)
            lines.append("FROM THE ERROR-FARE FEEDS")
            lines.append("=" * 62)
            for item in rss_items[:8]:
                tag = " [HOT]" if item.is_hot else ""
                price = f" (${item.price_usd:,.0f})" if item.price_usd else ""
                lines.append(f"\n * {item.title}{price}{tag}")
                lines.append(f"   {item.link}")
            lines.append("")

        lines.append("")
        lines.append("-" * 62)
        lines.append("Before you book:")
        lines.append("  - Multi-city itineraries are separate tickets. A missed")
        lines.append("    connection is on you, which is why the layover minimum")
        lines.append("    is a full day.")
        lines.append("  - Cached fares can vanish. Confirm the price on the")
        lines.append("    booking site before celebrating.")
        lines.append("  - On a suspected error fare: book direct with the airline")
        lines.append("    if you can, don't call to ask about it, and wait a week")
        lines.append("    before booking hotels.")
        return "\n".join(lines)

    @staticmethod
    def _basis_label(d: Deal) -> str:
        return {
            "history": f"this bot's own history ({d.observations} observations)",
            "google_typical": "Google's typical price range",
            "baseline": "your configured baseline",
            "ceiling": "hard price ceiling (no history yet)",
        }.get(d.reference_basis, d.reference_basis)

    # ---------- html ----------

    def _html_body(
        self, deals: Sequence[Deal], rss_items: Sequence[FeedItem], urgent: bool
    ) -> str:
        now = datetime.now().strftime("%A %B %d, %Y at %I:%M %p")
        parts: List[str] = [
            """<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
 body{margin:0;padding:0;background:#f1f5f9;
      font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
      color:#0f172a;}
 .wrap{max-width:640px;margin:0 auto;padding:16px;}
 .hdr{background:#0f172a;color:#fff;padding:20px;border-radius:10px 10px 0 0;}
 .hdr h1{margin:0;font-size:18px;letter-spacing:.02em;}
 .hdr p{margin:4px 0 0;font-size:13px;color:#94a3b8;}
 .card{background:#fff;border:1px solid #e2e8f0;border-top:none;padding:18px;}
 .card:last-of-type{border-radius:0 0 10px 10px;}
 .tier{display:inline-block;font-size:11px;font-weight:700;letter-spacing:.06em;
       padding:3px 8px;border-radius:4px;color:#fff;}
 .price{font-size:30px;font-weight:700;margin:10px 0 2px;}
 .route{font-size:16px;font-weight:600;color:#334155;}
 .meta{margin:12px 0;font-size:14px;color:#475569;}
 .meta div{padding:3px 0;}
 .lbl{display:inline-block;min-width:92px;color:#94a3b8;font-size:12px;
      text-transform:uppercase;letter-spacing:.04em;}
 .rec{background:#fef3c7;border-left:3px solid #d97706;padding:8px 12px;
      margin:12px 0;font-size:13px;color:#78350f;border-radius:0 4px 4px 0;}
 .itin{background:#f8fafc;border-radius:6px;padding:10px 12px;margin:12px 0;
       font-size:13px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;
       color:#334155;}
 .itin div{padding:2px 0;}
 .notes{font-size:12px;color:#64748b;margin:10px 0;padding-left:16px;}
 .cmp{width:100%;background:#f8fafc;border-radius:6px;margin:12px 0;
      border-collapse:separate;padding:4px 0;}
 .cmp td{padding:5px 12px;font-size:14px;}
 .cmp-l{color:#64748b;width:90px;}
 .cmp-v{text-align:right;color:#0f172a;font-size:16px;}
 .cmp-note{font-size:11px;color:#94a3b8;margin:-6px 0 10px;padding-left:2px;}
 .bag{background:#f8fafc;padding:8px 12px;margin:12px 0;font-size:12px;
      border-radius:0 4px 4px 0;color:#475569;}
 .bag ul{margin:4px 0 0;padding-left:16px;}
 .btn{display:inline-block;background:#0f172a;color:#fff !important;
      text-decoration:none;padding:11px 20px;border-radius:6px;
      font-weight:600;font-size:14px;margin-top:8px;}
 .rss{background:#fff;border:1px solid #e2e8f0;border-top:none;padding:18px;}
 .rss a{color:#0f172a;font-size:14px;}
 .foot{font-size:12px;color:#64748b;padding:18px;line-height:1.6;}
 .foot ul{margin:6px 0;padding-left:18px;}
</style></head><body><div class="wrap">"""
        ]

        headline = "Deals found" if not urgent else "Act fast"
        parts.append(
            f'<div class="hdr"><h1>{headline}</h1><p>{now}</p></div>'
        )

        for d in deals:
            color = TIER_COLOR.get(d.tier, "#475569")
            label = TIER_LABEL.get(d.tier, d.tier.upper())
            via = (
                f' <span style="color:#64748b">via {_esc(d.stopover_city)}</span>'
                if d.stopover_city
                else ""
            )
            parts.append('<div class="card">')
            parts.append(
                f'<span class="tier" style="background:{color}">{label}</span>'
            )
            parts.append(f'<div class="price">${d.total_price_usd:,.0f}</div>')
            if d.bag_fee_usd:
                parts.append(
                    f'<div style="font-size:13px;color:#64748b;margin:-4px 0 6px">'
                    f"${d.price_usd:,.0f} fare + ${d.bag_fee_usd:,.0f} carry-on "
                    f"across {d.ticket_count} ticket"
                    f"{'s' if d.ticket_count > 1 else ''}</div>"
                )
            parts.append(
                f'<div class="route">{_esc(d.origin)} &rarr; '
                f"{_esc(d.destination_city)}{via}</div>"
            )

            if d.typical_price:
                save = ""
                if d.savings_usd and d.savings_usd > 0:
                    save = (
                        f'<tr><td class="cmp-l">You save</td>'
                        f'<td class="cmp-v" style="color:#047857;font-weight:700">'
                        f"${d.savings_usd:,.0f} &middot; "
                        f"{d.savings_pct:.0f}% off normal</td></tr>"
                    )
                rng = ""
                if d.typical_low and d.typical_high:
                    rng = (
                        f'<div class="cmp-note">Google puts the typical range at '
                        f"${d.typical_low:,.0f}&ndash;${d.typical_high:,.0f}</div>"
                    )
                parts.append(
                    f'<table class="cmp">'
                    f'<tr><td class="cmp-l">Normally</td>'
                    f'<td class="cmp-v" style="text-decoration:line-through;'
                    f'color:#94a3b8">${d.typical_price:,.0f}</td></tr>'
                    f'<tr><td class="cmp-l">You pay</td>'
                    f'<td class="cmp-v" style="font-weight:700">'
                    f"${d.total_price_usd:,.0f}</td></tr>"
                    f"{save}</table>"
                    f'<div class="cmp-note">Based on {_esc(d.typical_basis)}</div>'
                    f"{rng}"
                )

            parts.append('<div class="meta">')
            parts.append(
                f'<div><span class="lbl">Depart</span>'
                f'{d.depart_date.strftime("%a %b %d, %Y")}</div>'
            )
            if d.return_date:
                parts.append(
                    f'<div><span class="lbl">Return</span>'
                    f'{d.return_date.strftime("%a %b %d, %Y")} '
                    f"&middot; {d.nights} nights</div>"
                )
            if d.stopover_city:
                days = (d.stopover_hours or 0) / 24
                parts.append(
                    f'<div><span class="lbl">Stopover</span>'
                    f"{days:.1f} days in {_esc(d.stopover_city)}</div>"
                )
            elif d.layover_hours is not None and d.layover_hours > 0:
                approx = "~" if d.layover_estimated else ""
                est = " (estimated)" if d.layover_estimated else ""
                parts.append(
                    f'<div><span class="lbl">Layover</span>'
                    f"{approx}{d.layover_hours:.1f}h{est}</div>"
                )
            elif d.layover_band == "quick" and d.layover_hours == 0:
                stops = d.legs[0].stops if d.legs else None
                if stops == 0:
                    parts.append(
                        '<div><span class="lbl">Layover</span>'
                        "none &mdash; nonstop</div>"
                    )
                else:
                    parts.append(
                        '<div><span class="lbl">Layover</span>'
                        "connection, length not reported &mdash; "
                        "<b>check before booking</b></div>"
                    )
            if d.airline:
                parts.append(
                    f'<div><span class="lbl">Airline</span>{_esc(d.airline)}</div>'
                )
            parts.append(
                f'<div><span class="lbl">Threshold</span>'
                f"{d.discount_pct:.0f}% below ${d.reference_price:,.0f}</div>"
            )
            parts.append(
                f'<div><span class="lbl">Benchmark</span>'
                f"{_esc(self._basis_label(d))}</div>"
            )
            parts.append("</div>")

            if d.is_record:
                if d.previous_record:
                    parts.append(
                        f'<div class="rec"><strong>New record.</strong> '
                        f"Previous low for this route: "
                        f"${d.previous_record:,.0f}</div>"
                    )
                else:
                    parts.append(
                        '<div class="rec"><strong>Lowest seen</strong> '
                        "for this route so far.</div>"
                    )

            if d.legs and d.stopover_code:
                parts.append('<div class="itin">')
                for leg in d.legs:
                    when = leg.depart_at.strftime("%b %d %H:%M")
                    price = f" &middot; ${leg.price_usd:,.0f}" if leg.price_usd else ""
                    parts.append(
                        f"<div>{_esc(leg.origin)} &rarr; {_esc(leg.destination)}"
                        f"&nbsp;&nbsp;{when}&nbsp;&nbsp;"
                        f"{_esc(leg.airline)}{_esc(leg.flight_number)}{price}</div>"
                    )
                parts.append("</div>")

            if d.bag_warnings:
                color, title = {
                    "oversize": ("#b91c1c", "Carry-on: likely a problem here"),
                    "tight": ("#c2410c", "Carry-on: possible problem"),
                    "low": ("#64748b", "Carry-on: low risk"),
                }.get(d.bag_risk, ("#475569", "Carry-on notes"))
                parts.append(
                    f'<div class="bag" style="border-left:3px solid {color}">'
                    f'<strong style="color:{color}">{title}</strong><ul>'
                )
                for w in d.bag_warnings:
                    parts.append(f"<li>{_esc(w)}</li>")
                parts.append("</ul></div>")

            if d.notes:
                parts.append('<ul class="notes">')
                for note in d.notes:
                    parts.append(f"<li>{_esc(note)}</li>")
                parts.append("</ul>")

            if d.booking_url:
                parts.append(
                    f'<a class="btn" href="{_esc(d.booking_url)}">View this fare</a>'
                )
            parts.append("</div>")

        if rss_items:
            parts.append('<div class="rss"><strong>From the error-fare feeds</strong>')
            for item in rss_items[:8]:
                price = f" (${item.price_usd:,.0f})" if item.price_usd else ""
                hot = " &middot; <strong>HOT</strong>" if item.is_hot else ""
                parts.append(
                    f'<div style="padding:6px 0"><a href="{_esc(item.link)}">'
                    f"{_esc(item.title)}</a>{price}{hot}</div>"
                )
            parts.append("</div>")

        parts.append(
            """<div class="foot"><strong>Before you book</strong>
<ul>
<li>Multi-city itineraries are separate tickets &mdash; a missed connection
is on you. That's why the layover minimum is a full day.</li>
<li>Cached fares can vanish. Confirm on the booking site.</li>
<li>Suspected error fare: book direct with the airline where possible,
don't call to ask about it, and wait a week before booking hotels.</li>
</ul></div></div></body></html>"""
        )
        return "".join(parts)


def _esc(text: Any) -> str:
    s = "" if text is None else str(text)
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
