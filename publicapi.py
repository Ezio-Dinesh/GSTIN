import os
import json
import time
import base64
import requests
import sys
from fastapi import FastAPI, HTTPException, Query, Header, Depends
from playwright.sync_api import sync_playwright
from dotenv import load_dotenv
from typing import Optional, List

load_dotenv()

# ---------- Environment ----------
API_KEY_2CAPTCHA = os.getenv('API_KEY_2CAPTCHA')
if not API_KEY_2CAPTCHA:
    raise ValueError("❌ Missing API_KEY_2CAPTCHA")

API_KEY = os.getenv('API_KEY')  # <-- Your API key for this service
if not API_KEY:
    raise ValueError("❌ Missing API_KEY for authentication")

# ---------- App ----------
app = FastAPI(title="GST Scraper API")

# ---------- Captcha Solver (original, reliable) ----------
def solve_captcha(b64_image, api_key):
    print("   ⏳ Sending screenshot to 2Captcha...")
    payload = {
        'method': 'base64',
        'key': api_key,
        'body': b64_image,
        'json': 1,
        'numeric': 1
    }
    res = requests.post('https://2captcha.com/in.php', data=payload).json()
    if res['status'] != 1:
        raise Exception(f"2Captcha submission error: {res['request']}")
    captcha_id = res['request']
    print(f"   ⏳ Submitted. Waiting for solution (ID: {captcha_id})...")
    for _ in range(30):
        time.sleep(5)
        poll = requests.get(
            f'https://2captcha.com/res.php?key={api_key}&action=get&id={captcha_id}&json=1'
        ).json()
        if poll['status'] == 1:
            return poll['request']
        elif poll['request'] != 'CAPCHA_NOT_READY':
            raise Exception(f"2Captcha error: {poll['request']}")
    raise Exception("Timeout waiting for captcha solution.")

# ---------- Scraper Class (headless, retries) ----------
class GSTSearchScraper:
    def __init__(self, gstin, api_key):
        self.gstin = gstin.strip()
        self.api_key = api_key

    def _get_text(self, element):
        try:
            return element.inner_text().strip()
        except:
            return ""

    def _extract_business_details(self, page):
        details = {}
        try:
            page.wait_for_selector('.tbl-format', state='visible', timeout=15000)
            container = page.locator('.tbl-format')

            def get_value_by_label(label_text):
                strong = container.locator(f'strong:has-text("{label_text}")')
                if strong.count() == 0:
                    return None
                parent = strong.locator('..')
                next_sibling = parent.locator('xpath=following-sibling::*[1]')
                if next_sibling.count() == 0:
                    return None
                tag = next_sibling.first.evaluate('el => el.tagName')
                if tag.lower() == 'p':
                    return self._get_text(next_sibling.first)
                elif tag.lower() == 'ul':
                    items = next_sibling.first.locator('li').all()
                    return [self._get_text(li) for li in items if self._get_text(li)]
                elif tag.lower() == 'a':
                    return "View link present"
                else:
                    return self._get_text(next_sibling.first)

            details['legalName'] = get_value_by_label("Legal Name of Business")
            details['tradeName'] = get_value_by_label("Trade Name")
            details['registrationDate'] = get_value_by_label("Effective Date of registration")
            details['constitution'] = get_value_by_label("Constitution of Business")
            details['status'] = get_value_by_label("GSTIN / UIN Status")
            details['taxpayerType'] = get_value_by_label("Taxpayer Type")
            details['administrativeOffice'] = get_value_by_label("Administrative Office") or []
            details['otherOffice'] = get_value_by_label("Other Office") or []
            details['principalPlace'] = get_value_by_label("Principal Place of Business")
            details['aadhaarAuthenticated'] = get_value_by_label("Whether Aadhaar Authenticated?")
            details['ekycVerified'] = get_value_by_label("Whether e-KYC Verified?")
            details['additionalTradeName'] = get_value_by_label("Additional Trade Name")

            if isinstance(details['administrativeOffice'], list):
                details['administrativeOffice'] = [i for i in details['administrativeOffice'] if i]
            if isinstance(details['otherOffice'], list):
                details['otherOffice'] = [i for i in details['otherOffice'] if i]

            return details
        except Exception as e:
            print(f"   ❌ Failed to extract business details: {e}")
            return None

    def _extract_filing_table(self, page):
        filing_data = {"GSTR3B": [], "GSTR1IFF": []}
        try:
            filing_btn = page.locator('#filingTable')
            for _ in range(30):
                if filing_btn.is_enabled():
                    break
                time.sleep(1)
            else:
                print("   ⚠️ Show Filing Table button remained disabled.")
                return filing_data

            filing_btn.click()
            page.wait_for_selector('select[name="fin"]', state='visible', timeout=10000)
            dropdown = page.locator('select[name="fin"]')
            options = dropdown.locator('option').all()
            if not options:
                print("   ⚠️ No financial years available.")
                return filing_data
            first_option = options[0]
            first_value = first_option.get_attribute('value')
            if first_value:
                dropdown.select_option(value=first_value)
                print(f"   ✅ Selected financial year: {first_option.inner_text().strip()}")
            else:
                return filing_data

            search_btn = page.locator('button.srchbtn[data-ng-click*="getFilingData"]')
            if search_btn.count() == 0:
                print("   ⚠️ Filing Search button not found.")
                return filing_data
            search_btn.click()
            page.wait_for_selector('.safariWid .col-sm-6 table', state='visible', timeout=15000)

            containers = page.locator('.safariWid .col-sm-6').all()
            for container in containers:
                heading = container.locator('h4')
                if heading.count() == 0:
                    continue
                heading_text = heading.first.inner_text().strip()
                if 'GSTR3B' in heading_text:
                    table_type = 'GSTR3B'
                elif 'GSTR-1/IFF' in heading_text:
                    table_type = 'GSTR1IFF'
                else:
                    continue

                table = container.locator('table')
                if table.count() == 0:
                    continue
                rows = table.locator('tbody tr').all()
                for row in rows:
                    cells = row.locator('td').all()
                    if len(cells) >= 4:
                        filing_data[table_type].append({
                            "financialYear": self._get_text(cells[0]),
                            "taxPeriod": self._get_text(cells[1]),
                            "dateOfFiling": self._get_text(cells[2]),
                            "status": self._get_text(cells[3])
                        })
                print(f"   ✅ Extracted {len(filing_data[table_type])} entries for {table_type}")
            return filing_data
        except Exception as e:
            print(f"   ❌ Failed to extract filing data: {e}")
            return filing_data

    def run(self):
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            print(f"\n🔍 Processing GSTIN: {self.gstin} (Attempt {attempt}/{max_attempts})")
            with sync_playwright() as p:
                browser = p.chromium.launch(
                    headless=True,          # <-- Cloud mode
                    args=['--disable-blink-features=AutomationControlled', '--no-sandbox']
                )
                context = browser.new_context(
                    user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
                    accept_downloads=True
                )
                page = context.new_page()
                try:
                    page.goto("https://services.gst.gov.in/services/searchtp", wait_until='domcontentloaded')
                    page.wait_for_selector('#for_gstin', state='visible', timeout=15000)
                    page.click('#for_gstin')
                    page.type('#for_gstin', self.gstin, delay=50)

                    page.wait_for_selector('div[data-captcha] img#imgCaptcha', state='visible', timeout=15000)
                    time.sleep(1)
                    captcha_element = page.locator('div[data-captcha] img#imgCaptcha')
                    img_bytes = captcha_element.screenshot(type='png')
                    b64_image = base64.b64encode(img_bytes).decode('utf-8')
                    captcha_text = solve_captcha(b64_image, self.api_key)
                    print(f"   ✅ Solved: {captcha_text}")
                    page.fill('#fo-captcha', captcha_text)

                    page.click('#lotsearch')
                    try:
                        page.wait_for_selector('.tbl-format', state='visible', timeout=20000)
                    except:
                        error_msg = ""
                        if page.locator('.err').count() > 0:
                            error_msg = page.locator('.err').inner_text().strip()
                        if error_msg:
                            print(f"   ❌ Search error: {error_msg}")
                            if 'captcha' in error_msg.lower():
                                print("   🔄 Wrong captcha, will retry.")
                                continue
                            else:
                                print("   ⚠️ Non-captcha error – aborting.")
                                return None
                        else:
                            print("   ❌ Search may have failed. No results found.")
                            return None

                    details = self._extract_business_details(page)
                    if details is None:
                        return None
                    filing_data = self._extract_filing_table(page)
                    result = {
                        "businessDetails": details,
                        "filingData": filing_data
                    }
                    print(f"   ✅ Successfully scraped data for {self.gstin}")
                    return result

                except Exception as e:
                    print(f"   ❌ Error processing {self.gstin}: {e}")
                    if attempt < max_attempts:
                        print("   🔄 Retrying after error...")
                        continue
                    else:
                        return None
                finally:
                    browser.close()
        print(f"❌ Failed to scrape {self.gstin} after {max_attempts} attempts.")
        return None

# ---------- Data Storage ----------
DATA_FILE = "scraped_data.json"

def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, 'r') as f:
            try:
                return json.load(f)
            except json.JSONDecodeError:
                return {}
    return {}

def save_data(data):
    with open(DATA_FILE, 'w') as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# ---------- Authentication ----------
def verify_api_key(api_key: str = Header(...)):
    if api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API Key")
    return api_key

# ---------- Endpoints ----------
@app.get("/")
def root():
    return {"message": "GST Scraper API is running. Use GET /scrape?gstin=..."}

@app.get("/scrape")
def scrape_gstin(
    gstin: str = Query(..., min_length=15, max_length=15, regex="^[A-Z0-9]{15}$"),
    api_key: str = Depends(verify_api_key)
):
    """
    Scrape GST details for a single GSTIN.
    Returns the scraped data and stores it in scraped_data.json.
    """
    scraper = GSTSearchScraper(gstin, API_KEY_2CAPTCHA)
    result = scraper.run()
    
    if result is None:
        raise HTTPException(status_code=404, detail="Scraping failed or GSTIN not found")
    
    # Store in JSON file
    data = load_data()
    data[gstin] = {
        "timestamp": time.time(),
        "data": result
    }
    save_data(data)
    
    return {
        "status": "success",
        "gstin": gstin,
        "data": result
    }

@app.get("/scrape/bulk")
def scrape_bulk(
    gstins: List[str] = Query(..., min_items=1, max_items=10),  # limit to avoid overload
    api_key: str = Depends(verify_api_key)
):
    """
    Scrape multiple GSTINs (max 10 per request).
    Returns a summary and stores all successfully scraped results.
    """
    results = {}
    failed = []
    data = load_data()
    
    for gstin in gstins:
        # Skip if already present? You can remove this check if you want to force refresh.
        if gstin in data:
            print(f"⏩ Skipping {gstin} – already scraped.")
            results[gstin] = "already scraped"
            continue
        
        scraper = GSTSearchScraper(gstin, API_KEY_2CAPTCHA)
        result = scraper.run()
        if result:
            data[gstin] = {
                "timestamp": time.time(),
                "data": result
            }
            results[gstin] = "success"
        else:
            failed.append(gstin)
    
    save_data(data)
    
    return {
        "status": "completed",
        "successful": list(results.keys()),
        "failed": failed
    }

@app.get("/data")
def get_all_data(api_key: str = Depends(verify_api_key)):
    """Return all stored scraped data."""
    return load_data()

@app.get("/data/{gstin}")
def get_gstin_data(gstin: str, api_key: str = Depends(verify_api_key)):
    """Return stored data for a specific GSTIN."""
    data = load_data()
    if gstin not in data:
        raise HTTPException(status_code=404, detail="GSTIN not found")
    return data[gstin]