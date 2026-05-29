from __future__ import annotations

import os
import re
import time
import json
from datetime import datetime
from urllib.parse import parse_qs, urlparse


CAROUSEL_SECTION_SELECTOR = "section.c-section--key-details"
TAB_SELLER_NOTES_SELECTOR = "#tab-seller-notes"
WHATSAPP_PHONE_RE = re.compile(r"(?:wa\.me/|phone=|tel:)(\+?\d+)", re.IGNORECASE)


class SharedBaseService:
    def _env_int(self, key: str, default: int, *, minimum: int | None = None) -> int:
        try:
            value = int((os.getenv(key) or str(default)).strip())
        except Exception:
            value = default
        if minimum is not None:
            value = max(minimum, value)
        return value

    def _env_float(self, key: str, default: float, *, minimum: float | None = None) -> float:
        try:
            value = float((os.getenv(key) or str(default)).strip())
        except Exception:
            value = default
        if minimum is not None:
            value = max(minimum, value)
        return value

    def _normalize_engine_cc(self, value: str | None) -> str | None:
        if not value:
            return None
        text = str(value).strip().lower()
        if not text:
            return None
        digits = re.findall(r"\d+", text.replace(",", ""))
        if not digits:
            return None
        return "".join(digits)

    def _page_has_tokens_botasaurus(self, driver, tokens: tuple[str, ...]) -> bool:
        try:
            title = driver.run_js("return (document.title || '').toLowerCase();")
        except Exception:
            title = ""
        try:
            text = driver.run_js("return ((document.body && document.body.innerText) || '').toLowerCase();")
        except Exception:
            try:
                text = (driver.page_html or "").lower()
            except Exception:
                text = ""
        haystack = f"{title}\n{text}".lower()
        return any(token in haystack for token in tokens)

    def _get_page_title_botasaurus(self, driver) -> str:
        try:
            title = driver.run_js("return (document.title || '').trim();")
        except Exception:
            title = ""
        return str(title or "").strip()

    def _get_page_text_botasaurus(self, driver) -> str:
        try:
            text = driver.run_js("return ((document.body && document.body.innerText) || '').trim();")
        except Exception:
            try:
                text = str(driver.page_html or "")
            except Exception:
                text = ""
        return str(text or "").strip()

    def _text_by_xpath_botasaurus(self, driver, xpath: str) -> str | None:
        try:
            text = driver.run_js(
                """
                const el = document.evaluate(
                  args.xpath,
                  document,
                  null,
                  XPathResult.FIRST_ORDERED_NODE_TYPE,
                  null
                ).singleNodeValue;
                if (!el) return null;
                const raw = (el.innerText || el.textContent || '').trim();
                return raw || null;
                """,
                {"xpath": xpath},
            )
        except Exception:
            text = None
        return text or None

    def _is_http_error_page_botasaurus(self, driver) -> bool:
        return self._page_has_tokens_botasaurus(
            driver,
            (
                "http error 400",
                "400 bad request",
                "request header or cookie too large",
                "this page isn't working",
                "this page isnt working",
                "this site can't be reached",
                "this site cant be reached",
                "err_tunnel_connection_failed",
                "tunnel_connection_failed",
                "err_proxy_connection_failed",
            ),
        )

    def _is_soft_block_page_botasaurus(self, driver) -> bool:
        return self._page_has_tokens_botasaurus(
            driver,
            (
                "verify you are human",
                "press and hold",
                "checking if the site connection is secure",
                "complete the security check",
                "enable javascript and cookies to continue",
                "access denied",
                "your request has been blocked",
                "request blocked",
                "temporarily unavailable",
                "unusual traffic",
                "captcha",
            ),
        )

    def close(self) -> None:
        return

    def stop_scraping(self) -> None:
        self.stop_flag = True

    def _get_attr_by_xpath_botasaurus(self, driver, xpath: str, attr: str) -> str | None:
        try:
            val = driver.run_js(
                """
                const el = document.evaluate(
                  args.xpath,
                  document,
                  null,
                  XPathResult.FIRST_ORDERED_NODE_TYPE,
                  null
                ).singleNodeValue;
                if (!el) return null;
                const v = el.getAttribute(args.attr);
                return (v || '').trim() || null;
                """,
                {"xpath": xpath, "attr": attr},
            )
        except Exception:
            val = None
        return val or None

    def _get_text_by_xpath_botasaurus(self, driver, xpath: str) -> str | None:
        try:
            text = driver.run_js(
                """
                const el = document.evaluate(
                  args.xpath,
                  document,
                  null,
                  XPathResult.FIRST_ORDERED_NODE_TYPE,
                  null
                ).singleNodeValue;
                if (!el) return null;
                const raw = (el.innerText || el.textContent || '').trim();
                return raw || null;
                """,
                {"xpath": xpath},
            )
        except Exception:
            text = None
        return text or None

    def _parse_int_from_text(self, text: str | None) -> int | None:
        if not text:
            return None
        digits = re.findall(r"\d+", str(text))
        if not digits:
            return None
        try:
            return int("".join(digits))
        except Exception:
            return None

    def _parse_rupiah_price(self, text: object | None) -> int | None:
        if isinstance(text, (int, float)):
            value = int(text)
            return value if 0 < value <= 100_000_000_000 else None
        raw = str(text or "").strip()
        if not raw:
            return None
        if re.search(r"https?://|icarcdn|\.webp|\.jpg|\.png", raw, re.IGNORECASE):
            return None
        decimal_match = re.fullmatch(r"\d+\.\d{1,2}", raw)
        if decimal_match:
            value = int(float(raw))
            return value if 0 < value <= 100_000_000_000 else None
        if ("rp" not in raw.lower()) and ("rm" not in raw.lower()) and not re.search(r"\d[\d.,]{5,}", raw):
            return None
        digits = re.findall(r"\d+", raw)
        if not digits:
            return None
        try:
            value = int("".join(digits))
        except Exception:
            return None
        return value if 0 < value <= 100_000_000_000 else None

    def _json_ld_objects_botasaurus(self, driver) -> list[dict]:
        try:
            raw_items = driver.run_js(
                """
                return Array.from(document.querySelectorAll('script[type="application/ld+json"]'))
                  .map((el) => el.textContent || '')
                  .filter(Boolean);
                """
            )
        except Exception:
            raw_items = []

        objects: list[dict] = []

        def add_obj(value) -> None:
            if isinstance(value, dict):
                objects.append(value)
                graph = value.get("@graph")
                if isinstance(graph, list):
                    for item in graph:
                        add_obj(item)
            elif isinstance(value, list):
                for item in value:
                    add_obj(item)

        for raw in raw_items or []:
            try:
                add_obj(json.loads(str(raw)))
            except Exception:
                continue
        return objects

    def _first_meta_content_botasaurus(self, driver, names: list[str]) -> str | None:
        ordered_names = list(dict.fromkeys(str(name or "").strip().lower() for name in names if str(name or "").strip()))
        if not ordered_names:
            return None
        try:
            value = driver.run_js(
                """
                const names = args.names.map((s) => String(s || '').trim().toLowerCase());
                const metas = Array.from(document.querySelectorAll('meta'));
                for (const name of names) {
                  for (const meta of metas) {
                    const keys = [
                      meta.getAttribute('name'),
                      meta.getAttribute('property'),
                      meta.getAttribute('itemprop')
                    ].map((s) => String(s || '').trim().toLowerCase());
                    if (!keys.includes(name)) continue;
                    const content = String(meta.getAttribute('content') || '').trim();
                    if (content) return content;
                  }
                }
                return null;
                """,
                {"names": ordered_names},
            )
            if value:
                return str(value).strip() or None
        except Exception:
            pass

        try:
            html = str(driver.page_html or "")
        except Exception:
            html = ""
        if not html:
            return None

        meta_re = re.compile(r"<meta\b(?P<attrs>[^>]*?)>", re.IGNORECASE | re.DOTALL)
        attr_re = re.compile(r"([:\w-]+)\s*=\s*(['\"])(.*?)\2", re.IGNORECASE | re.DOTALL)
        metas = []
        for match in meta_re.finditer(html):
            metas.append({key.lower(): value for key, _, value in attr_re.findall(match.group("attrs") or "")})
        for name in ordered_names:
            for attrs in metas:
                keys = {
                    str(attrs.get("name") or "").strip().lower(),
                    str(attrs.get("property") or "").strip().lower(),
                    str(attrs.get("itemprop") or "").strip().lower(),
                }
                if name not in keys:
                    continue
                content = re.sub(r"\s+", " ", str(attrs.get("content") or "")).strip()
                if content:
                    return content
        return None

    def _first_json_ld_value(self, items: list[dict], key: str) -> str | None:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            value = item.get(key)
            if isinstance(value, dict):
                value = value.get("name") or value.get("value") or value.get("engineType")
            text = re.sub(r"\s+", " ", str(value or "")).strip()
            if text:
                return text
        return None

    def _normalize_condition_value(self, value: str | None) -> str | None:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            return None
        lower = text.lower()
        if "used" in lower:
            return "Used Car"
        if "recon" in lower:
            return "Recon Car"
        if "new" in lower:
            return "New Car"
        return text[:50]

    def _clean_short_text(self, value: object | None, *, max_len: int = 100) -> str | None:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            return None
        if re.search(r"https?://|icarcdn|\.webp|\.jpg|\.png", text, re.IGNORECASE):
            return None
        if len(text) > max_len:
            return None
        return text

    def _clean_variant_text(self, value: object | None, *, max_len: int = 100) -> str | None:
        text = self._clean_short_text(value, max_len=max_len)
        if not text:
            return None
        if text.strip().lower() in {"new", "new car", "used", "used car", "recon", "recon car"}:
            return None
        return text

    def _clean_year(self, value: object | None) -> int | None:
        parsed = self._parse_int_from_text(value)
        if parsed is None:
            return None
        current_year = datetime.now().year + 1
        return parsed if 1900 <= parsed <= current_year else None

    def _parse_mileage(self, mileage_str: str | None) -> int:
        if not mileage_str or mileage_str.strip() == "- km":
            return 0
        try:
            if "-" in mileage_str:
                right = mileage_str.split("-")[-1].strip()
            else:
                right = mileage_str.strip()
            right = right.replace("km", "").replace("KM", "").replace("Km", "").strip()
            right = right.replace("K", "000").replace("k", "000")
            right = right.replace(" ", "")
            return int(re.sub(r"[^\d]", "", right))
        except Exception:
            return 0

    def _spec_key_to_snake(self, label: str) -> str:
        raw = str(label or "").strip().lower()
        if not raw:
            return ""
        raw = raw.replace("&", " and ")
        raw = re.sub(r"[()]+", " ", raw)
        raw = re.sub(r"[^a-z0-9]+", "_", raw).strip("_")
        raw = re.sub(r"_+", "_", raw)
        return raw

    def _extract_carousel_dict_botasaurus(self, driver) -> dict[str, object] | None:
        """
        Extract key/value pairs from the carousel (key details cards).
        Label class: 'u-text-7  u-color-muted  u-block'
        Value class: 'u-text-bold  u-block'
        """
        try:
            items = driver.run_js(
                """
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                const root = document.querySelector(args.selector);
                if (!root) return [];

                const cards = Array.from(root.querySelectorAll('.c-key-details__item, .c-card'));
                const out = [];
                for (const el of cards) {
                  const labelEl = el.querySelector('span.u-text-7.u-color-muted.u-block');
                  const valueEl = el.querySelector('span.u-text-bold.u-block');
                  const label = norm(labelEl && (labelEl.textContent || labelEl.innerText));
                  const value = norm(valueEl && (valueEl.textContent || valueEl.innerText));
                  if (!label || !value) continue;
                  out.push({ label, value });
                }
                return out;
                """,
                {"selector": CAROUSEL_SECTION_SELECTOR},
            )
        except Exception:
            items = []

        data: dict[str, object] = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "") or "").strip()
            value = str(item.get("value", "") or "").strip()
            if not label or not value:
                continue
            key = self._spec_key_to_snake(label)
            if not key:
                continue
            if key not in data:
                data[key] = value
                continue
            existing = data[key]
            if isinstance(existing, list):
                if value not in existing:
                    existing.append(value)
                continue
            if existing != value:
                data[key] = [existing, value]
        return data or None

    def _extract_seller_notes_text_botasaurus(self, driver) -> str | None:
        try:
            return driver.run_js(
                """
                const norm = (s) => (s || '').trim();
                const tab = document.querySelector(args.selector);
                if (!tab) return null;
                const desc = tab.querySelector('[itemprop=\"description\"]') || tab;
                const text = norm(desc && (desc.innerText || desc.textContent));
                return text || null;
                """,
                {"selector": TAB_SELLER_NOTES_SELECTOR},
            )
        except Exception:
            return None

    def _click_tab_by_href_botasaurus(self, driver, *, href: str) -> bool:
        """
        Some tabs render content lazily only after being activated. Click the tab anchor by href.
        Example href: "#tab-specifications"
        """
        try:
            return bool(
                driver.run_js(
                    """
                    const href = String(args.href || '').trim();
                    if (!href) return false;
                    const links = Array.from(document.querySelectorAll('a.c-tab__item[data-toggle=\"tab\"][href]'));
                    const a = links.find((el) => String(el.getAttribute('href') || '').trim() === href);
                    if (!a) return false;
                    try { a.scrollIntoView({block: 'center'}); } catch (e) {}
                    a.click();
                    return true;
                    """,
                    {"href": href},
                )
            )
        except Exception:
            return False

    def _extract_tab_grouped_kv_botasaurus(self, driver, *, tab_selector: str) -> dict[str, object] | None:
        """
        Extract grouped key/value pairs from Specifications / Equipments tabs.
        - Category: <h3 class="u-text-6 u-text-uppercase ...">...</h3>
        - Row label: <span class="u-width-1/2">Key</span>
        - Row value: <span class="u-text-bold u-width-1/2 u-align-right">Value</span>
        """
        try:
            items = driver.run_js(
                """
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                const tab = document.querySelector(args.selector);
                if (!tab) return [];

                const groups = Array.from(tab.querySelectorAll('.o-grid')).filter((g) => g.querySelector('h3'));
                const out = [];
                for (const g of groups) {
                  const h3 = g.querySelector('h3');
                  const category = norm(h3 && (h3.textContent || h3.innerText));
                  if (!category) continue;
                  const rows = Array.from(g.querySelectorAll('div.u-flex.u-flex--justify-between'));
                  for (const row of rows) {
                    // NOTE: class "u-width-1/2" contains "/" which breaks CSS selectors unless escaped.
                    // Avoid brittle selectors and instead pick the bold span as value, and the other span as label.
                    const spans = Array.from(row.querySelectorAll('span'));
                    const valueEl = spans.find((s) => s && s.classList && s.classList.contains('u-text-bold')) || null;
                    const labelEl = spans.find((s) => s && s !== valueEl) || null;
                    const label = norm(labelEl && (labelEl.textContent || labelEl.innerText));
                    const value = norm(valueEl && (valueEl.textContent || valueEl.innerText));
                    if (!label || !value) continue;
                    out.push({ category, label, value });
                  }
                }
                return out;
                """,
                {"selector": tab_selector},
            )
        except Exception:
            items = []

        grouped: dict[str, object] = {}
        for item in items or []:
            if not isinstance(item, dict):
                continue
            category = str(item.get("category", "") or "").strip()
            label = str(item.get("label", "") or "").strip()
            value = str(item.get("value", "") or "").strip()
            if not category or not label or not value:
                continue
            cat_key = self._spec_key_to_snake(category)
            key = self._spec_key_to_snake(label)
            if not cat_key or not key:
                continue
            cat_obj = grouped.get(cat_key)
            if not isinstance(cat_obj, dict):
                cat_obj = {}
                grouped[cat_key] = cat_obj

            if key not in cat_obj:
                cat_obj[key] = value
                continue
            existing = cat_obj[key]
            if isinstance(existing, list):
                if value not in existing:
                    existing.append(value)
                continue
            if existing != value:
                cat_obj[key] = [existing, value]

        return grouped or None

    def _normalize_whatsapp_number(self, value: str | None) -> str | None:
        text = str(value or "").strip()
        if not text:
            return None
        digits = re.sub(r"\D+", "", text)
        if not digits:
            return None
        if digits.startswith("0") and len(digits) >= 9:
            return f"60{digits[1:]}"
        return digits

    def _extract_whatsapp_numbers_from_candidates(self, candidates: list[dict] | None) -> list[str] | None:
        seen: set[str] = set()
        numbers: list[str] = []
        for item in candidates or []:
            if not isinstance(item, dict):
                continue

            direct = self._normalize_whatsapp_number(item.get("data_whatsapp_number"))
            if direct and direct not in seen:
                seen.add(direct)
                numbers.append(direct)

            direct_phone = self._normalize_whatsapp_number(item.get("data_phone"))
            if direct_phone and direct_phone not in seen:
                seen.add(direct_phone)
                numbers.append(direct_phone)

            href = str(item.get("href") or "").strip()
            if not href:
                continue

            parsed_phone = None
            try:
                parsed = urlparse(href)
                query_phone = parse_qs(parsed.query).get("phone")
                if query_phone:
                    parsed_phone = query_phone[0]
            except Exception:
                parsed_phone = None

            phone = self._normalize_whatsapp_number(parsed_phone)
            if not phone:
                match = WHATSAPP_PHONE_RE.search(href)
                phone = self._normalize_whatsapp_number(match.group(1) if match else None)
            if phone and phone not in seen:
                seen.add(phone)
                numbers.append(phone)
        return numbers or None

    def _extract_whatsapp_candidates_botasaurus(self, driver, *, listing_id: str, modal_only: bool = False) -> list[dict] | None:
        listing_id = str(listing_id or "").strip()
        if not listing_id:
            return None
        try:
            items = driver.run_js(
                """
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                const listingId = String(args.listingId || '').trim();
                if (!listingId) return [];
                const root = args.modalOnly ? (document.querySelector('#modal-contact-seller') || document) : document;
                const nodes = Array.from(root.querySelectorAll('a[href], button, [role="button"]'));
                const out = [];
                for (const el of nodes) {
                  const dataListingId = String(el.getAttribute('data-listing-id') || '').trim();
                  const dataTmlSi = String(el.getAttribute('data-tml-si') || '').trim();
                  const href = el.href || el.getAttribute('href') || '';
                  const dataWhatsappNumber = el.getAttribute('data-whatsapp-number') || '';
                  const dataPhone = el.getAttribute('data-phone') || '';
                  const dataPhones = el.getAttribute('data-phones') || '';
                  const text = norm(el.textContent || el.innerText);
                  const cls = String(el.className || '');
                  const dataTarget = el.getAttribute('data-target') || '';
                  const dataToggle = el.getAttribute('data-toggle') || '';
                  const dataGaEvent = String(el.getAttribute('data-ga-event') || '');
                  const isModalPhoneLink = !!args.modalOnly && /^tel:/i.test(href);
                  const looksWhatsapp =
                    /whatsapp/i.test(text) ||
                    /whatsapp/i.test(cls) ||
                    /api\.whatsapp\.com/i.test(href) ||
                    /wa\.me/i.test(href) ||
                    !!dataWhatsappNumber ||
                    /whatsapp/i.test(dataGaEvent) ||
                    isModalPhoneLink;
                  if (!looksWhatsapp) continue;
                  if (dataListingId && dataListingId !== listingId) continue;
                  if (!dataListingId && dataTmlSi && dataTmlSi !== listingId) continue;
                  out.push({
                    href,
                    text,
                    class_name: cls,
                    data_whatsapp_number: dataWhatsappNumber || null,
                    data_phone: dataPhone || dataPhones || null,
                    data_listing_id: dataListingId || null,
                    data_tml_si: dataTmlSi || null,
                    data_target: dataTarget || null,
                    data_toggle: dataToggle || null,
                    data_ga_event: dataGaEvent || null,
                  });
                }
                return out;
                """,
                {"listingId": listing_id, "modalOnly": bool(modal_only)},
            )
        except Exception:
            items = []
        return items or None

    def _click_whatsapp_button_botasaurus(self, driver, *, listing_id: str) -> bool:
        listing_id = str(listing_id or "").strip()
        if not listing_id:
            return False
        try:
            return bool(
                driver.run_js(
                    """
                    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                    const listingId = String(args.listingId || '').trim();
                    const nodes = Array.from(document.querySelectorAll('a[href], button, [role="button"]'));
                    const candidates = nodes.map((el) => {
                      const dataListingId = String(el.getAttribute('data-listing-id') || '').trim();
                      const dataTmlSi = String(el.getAttribute('data-tml-si') || '').trim();
                      if (dataListingId && dataListingId !== listingId) return null;
                      if (!dataListingId && dataTmlSi && dataTmlSi !== listingId) return null;
                      const text = norm(el.textContent || el.innerText);
                      const cls = String(el.className || '').toLowerCase();
                      const href = String(el.href || el.getAttribute('href') || '').trim().toLowerCase();
                      const dataTarget = String(el.getAttribute('data-target') || '').trim();
                      const dataWhatsappNumber = String(el.getAttribute('data-whatsapp-number') || '').trim();
                      const dataGaEvent = String(el.getAttribute('data-ga-event') || '').trim().toLowerCase();
                      let score = 0;
                      if (cls.includes('jump-whatsapp')) score += 100;
                      if (cls.includes('whatsapp_name')) score += 90;
                      if (dataWhatsappNumber) score += 80;
                      if (href.includes('api.whatsapp.com') || href.includes('wa.me')) score += 70;
                      if (text.includes('whatsapp')) score += 60;
                      if (cls.includes('whatsapp')) score += 50;
                      if (dataGaEvent.includes('whatsapp')) score += 40;
                      if (dataTarget === '#modal-contact-seller') score += 5;
                      if (score <= 0) return null;
                      return { el, score, isAnchor: el.tagName === 'A' ? 1 : 0 };
                    }).filter(Boolean);
                    if (!candidates.length) return false;
                    candidates.sort((a, b) => (b.score - a.score) || (b.isAnchor - a.isAnchor));
                    for (const item of candidates) {
                      const el = item.el;
                      try { el.scrollIntoView({block: 'center'}); } catch (e) {}
                      try {
                        el.click();
                        return true;
                      } catch (e) {}
                    }
                    return false;
                    """,
                    {"listingId": listing_id},
                )
            )
        except Exception:
            return False

    def _close_contact_seller_modal_botasaurus(self, driver) -> bool:
        try:
            return bool(
                driver.run_js(
                    """
                    const modal = document.querySelector('#modal-contact-seller');
                    if (!modal) return false;
                    const btn =
                      modal.querySelector('.js-modal-destroy') ||
                      modal.querySelector('[data-dismiss="modal"]') ||
                      modal.querySelector('button');
                    if (!btn) return false;
                    try { btn.scrollIntoView({block: 'center'}); } catch (e) {}
                    try { btn.click(); } catch (e) { return false; }
                    return true;
                    """
                )
            )
        except Exception:
            return False

    def _extract_whatsapp_numbers_botasaurus(self, driver, *, listing_id: str, modal_wait_seconds: float = 4.0) -> list[str] | None:
        numbers = self._extract_whatsapp_numbers_from_candidates(
            self._extract_whatsapp_candidates_botasaurus(driver, listing_id=listing_id, modal_only=False)
        )
        if numbers:
            return numbers

        if not self._click_whatsapp_button_botasaurus(driver, listing_id=listing_id):
            return None

        deadline = time.time() + max(0.0, modal_wait_seconds)
        while time.time() < deadline:
            time.sleep(0.25)
            numbers = self._extract_whatsapp_numbers_from_candidates(
                self._extract_whatsapp_candidates_botasaurus(driver, listing_id=listing_id, modal_only=True)
            )
            if numbers:
                self._close_contact_seller_modal_botasaurus(driver)
                return numbers
        self._close_contact_seller_modal_botasaurus(driver)
        return None

    def _normalize_contact_seller(self, value: str | None) -> str | None:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        if not text:
            return None
        if len(re.sub(r"\D+", "", text)) < 8:
            return None
        return text

    def _extract_contact_seller_candidates_botasaurus(
        self, driver, *, listing_id: str, modal_only: bool = False
    ) -> list[dict] | None:
        listing_id = str(listing_id or "").strip()
        if not listing_id:
            return None
        try:
            items = driver.run_js(
                """
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                const listingId = String(args.listingId || '').trim();
                if (!listingId) return [];
                const root = args.modalOnly ? (document.querySelector('#modal-contact-seller') || document) : document;
                const nodes = Array.from(root.querySelectorAll('a[href], button, [role="button"]'));
                const out = [];
                for (const el of nodes) {
                  const dataListingId = String(el.getAttribute('data-listing-id') || '').trim();
                  const dataTmlSi = String(el.getAttribute('data-tml-si') || '').trim();
                  if (dataListingId && dataListingId !== listingId) continue;
                  if (!dataListingId && dataTmlSi && dataTmlSi !== listingId) continue;
                  const href = el.href || el.getAttribute('href') || '';
                  const text = norm(el.textContent || el.innerText);
                  const cls = String(el.className || '');
                  const dataTarget = el.getAttribute('data-target') || '';
                  const dataToggle = el.getAttribute('data-toggle') || '';
                  const dataPhone = el.getAttribute('data-phone') || '';
                  const dataPhones = el.getAttribute('data-phones') || '';
                  const dataGaEvent = String(el.getAttribute('data-ga-event') || '');
                  const looksPhone =
                    /contact/i.test(text) ||
                    /call now/i.test(text) ||
                    /phone/i.test(cls) ||
                    /jump-phone/i.test(cls) ||
                    /^tel:/i.test(href) ||
                    !!dataPhone ||
                    !!dataPhones ||
                    /ctr_call/i.test(dataGaEvent);
                  if (!looksPhone) continue;
                  out.push({
                    href,
                    text,
                    class_name: cls,
                    data_phone: dataPhone || dataPhones || null,
                    data_listing_id: dataListingId || null,
                    data_tml_si: dataTmlSi || null,
                    data_target: dataTarget || null,
                    data_toggle: dataToggle || null,
                    data_ga_event: dataGaEvent || null,
                  });
                }
                return out;
                """,
                {"listingId": listing_id, "modalOnly": bool(modal_only)},
            )
        except Exception:
            items = []
        return items or None

    def _extract_contact_seller_from_candidates(self, candidates: list[dict] | None) -> str | None:
        for item in candidates or []:
            if not isinstance(item, dict):
                continue
            text_value = self._normalize_contact_seller(item.get("text"))
            if text_value:
                return text_value

            phone_value = self._normalize_contact_seller(item.get("data_phone"))
            if phone_value:
                return phone_value

            href = str(item.get("href") or "").strip()
            if href.lower().startswith("tel:"):
                href_value = self._normalize_contact_seller(href.split(":", 1)[-1])
                if href_value:
                    return href_value
        return None

    def _click_contact_button_botasaurus(self, driver, *, listing_id: str) -> bool:
        listing_id = str(listing_id or "").strip()
        if not listing_id:
            return False
        try:
            return bool(
                driver.run_js(
                    """
                    const norm = (s) => (s || '').replace(/\s+/g, ' ').trim().toLowerCase();
                    const listingId = String(args.listingId || '').trim();
                    const nodes = Array.from(document.querySelectorAll('a[href], button, [role="button"]'));
                    const candidates = nodes.map((el) => {
                      const dataListingId = String(el.getAttribute('data-listing-id') || '').trim();
                      const dataTmlSi = String(el.getAttribute('data-tml-si') || '').trim();
                      if (dataListingId && dataListingId !== listingId) return null;
                      if (!dataListingId && dataTmlSi && dataTmlSi !== listingId) return null;
                      const text = norm(el.textContent || el.innerText);
                      const cls = String(el.className || '').toLowerCase();
                      const href = String(el.href || el.getAttribute('href') || '').trim().toLowerCase();
                      const dataPhone = String(el.getAttribute('data-phone') || el.getAttribute('data-phones') || '').trim();
                      const dataGaEvent = String(el.getAttribute('data-ga-event') || '').trim().toLowerCase();
                      let score = 0;
                      if (cls.includes('jump-phone')) score += 100;
                      if (dataGaEvent.includes('ctr_call')) score += 80;
                      if (dataPhone) score += 60;
                      if (href.startsWith('tel:')) score += 50;
                      if (text.includes('contact')) score += 40;
                      if (text.includes('call now')) score += 30;
                      if (cls.includes('phone')) score += 20;
                      if (score <= 0) return null;
                      return { el, score, isAnchor: el.tagName === 'A' ? 1 : 0 };
                    }).filter(Boolean);
                    if (!candidates.length) return false;
                    candidates.sort((a, b) => (b.score - a.score) || (b.isAnchor - a.isAnchor));
                    for (const item of candidates) {
                      const el = item.el;
                      try { el.scrollIntoView({block: 'center'}); } catch (e) {}
                      try {
                        el.click();
                        return true;
                      } catch (e) {}
                    }
                    return false;
                    """,
                    {"listingId": listing_id},
                )
            )
        except Exception:
            return False

    def _extract_contact_seller_botasaurus(self, driver, *, listing_id: str, modal_wait_seconds: float = 3.0) -> str | None:
        if not self._click_contact_button_botasaurus(driver, listing_id=listing_id):
            return None

        deadline = time.time() + max(0.0, modal_wait_seconds)
        while time.time() < deadline:
            time.sleep(0.25)
            contact = self._extract_contact_seller_from_candidates(
                self._extract_contact_seller_candidates_botasaurus(driver, listing_id=listing_id, modal_only=True)
            )
            if contact:
                self._close_contact_seller_modal_botasaurus(driver)
                return contact
        self._close_contact_seller_modal_botasaurus(driver)
        return None

    def _extract_inline_seller_name_botasaurus(self, driver, *, listing_id: str) -> str | None:
        listing_id = str(listing_id or "").strip()
        if not listing_id:
            return None
        try:
            return driver.run_js(
                """
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                const listingId = String(args.listingId || '').trim();
                if (!listingId) return null;

                const scoreHeading = (el) => {
                  if (!el) return -1;
                  const text = norm(el.textContent || el.innerText);
                  if (!text) return -1;
                  const lower = text.toLowerCase();
                  if (lower === 'contact' || lower === 'whatsapp' || lower === 'save') return -1;
                  if (lower === 'transmission' || lower === 'mileage' || lower === 'color' || lower === 'condition') {
                    return -1;
                  }
                  if (/^rm\s*[\d,]+/i.test(text)) return -1;
                  if (/^[\d\s+\-()]+$/.test(text)) return -1;
                  let score = 0;
                  if (el.tagName === 'H6') score += 100;
                  if ((el.className || '').includes('u-margin-bottom-xxs')) score += 30;
                  if (text.length >= 4) score += 10;
                  if (!/\d{3,}/.test(text)) score += 5;
                  return score;
                };

                const roots = [];
                const pushRoot = (node) => {
                  if (!node) return;
                  if (roots.includes(node)) return;
                  roots.push(node);
                };

                const ctas = Array.from(document.querySelectorAll('a[href], button, [role="button"]')).filter((el) => {
                  const dataListingId = String(el.getAttribute('data-listing-id') || '').trim();
                  const dataTmlSi = String(el.getAttribute('data-tml-si') || '').trim();
                  if (dataListingId && dataListingId !== listingId) return false;
                  if (!dataListingId && dataTmlSi && dataTmlSi !== listingId) return false;
                  const text = norm(el.textContent || el.innerText).toLowerCase();
                  const cls = String(el.className || '').toLowerCase();
                  return (
                    text.includes('contact') ||
                    text.includes('whatsapp') ||
                    cls.includes('jump-phone') ||
                    cls.includes('jump-whatsapp')
                  );
                });

                for (const cta of ctas) {
                  let node = cta;
                  for (let i = 0; i < 6 && node; i++) {
                    node = node.parentElement;
                    if (!node) break;
                    const hasCandidateHeading = !!node.querySelector('h6.u-margin-bottom-xxs, h6');
                    const hasContactButtons = Array.from(node.querySelectorAll('a[href], button, [role="button"]')).some((el) => {
                      const text = norm(el.textContent || el.innerText).toLowerCase();
                      const cls = String(el.className || '').toLowerCase();
                      return (
                        text.includes('contact') ||
                        text.includes('whatsapp') ||
                        cls.includes('jump-phone') ||
                        cls.includes('jump-whatsapp')
                      );
                    });
                    if (hasCandidateHeading && hasContactButtons) {
                      pushRoot(node);
                    }
                  }
                }

                for (const root of roots) {
                  const headings = Array.from(root.querySelectorAll('h6.u-margin-bottom-xxs, h6'))
                    .filter((el) => scoreHeading(el) > 0)
                    .sort((a, b) => scoreHeading(b) - scoreHeading(a));
                  if (headings.length) {
                    return norm(headings[0].textContent || headings[0].innerText) || null;
                  }
                }
                return null;
                """,
                {"listingId": listing_id},
            )
        except Exception:
            return None

    def _extract_modal_seller_name_botasaurus(self, driver) -> str | None:
        try:
            return driver.run_js(
                """
                const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
                const modal = document.querySelector('#modal-contact-seller');
                if (!modal) return null;
                const headings = Array.from(modal.querySelectorAll('h6.u-margin-bottom-xxs, h6'));
                for (const heading of headings) {
                  const text = norm(heading.textContent || heading.innerText);
                  if (!text) continue;
                  const lower = text.toLowerCase();
                  if (lower === 'contact' || lower === 'whatsapp' || lower === 'save') continue;
                  if (/^[\d\s+\-()]+$/.test(text)) continue;
                  return text;
                }
                return null;
                """
            )
        except Exception:
            return None

    def _extract_seller_name_botasaurus(self, driver, *, listing_id: str, modal_wait_seconds: float = 3.0) -> str | None:
        name = self._extract_inline_seller_name_botasaurus(driver, listing_id=listing_id)
        if name:
            return name

        opened = self._click_whatsapp_button_botasaurus(driver, listing_id=listing_id)
        if not opened:
            opened = self._click_contact_button_botasaurus(driver, listing_id=listing_id)
        if not opened:
            return None

        deadline = time.time() + max(0.0, modal_wait_seconds)
        while time.time() < deadline:
            time.sleep(0.25)
            name = self._extract_modal_seller_name_botasaurus(driver)
            if name:
                self._close_contact_seller_modal_botasaurus(driver)
                return name
        self._close_contact_seller_modal_botasaurus(driver)
        return None
