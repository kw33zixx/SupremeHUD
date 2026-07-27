import time
from bs4 import BeautifulSoup
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium_stealth import stealth
import os
import json


def get_driver():
    options = Options()

    options.add_argument("--headless")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")

    options.add_experimental_option("excludeSwitches", ["enable-logging"])

    driver = webdriver.Chrome(options=options)

    stealth(
        driver,
        languages=["en-US", "en"],
        vendor="Google Inc.",
        platform="Win32",
        webgl_vendor="Intel Inc.",
        renderer="Intel Iris OpenGL Engine",
        fix_hairline=True,
    )

    return driver


def is_pure_numeric(value_str):
    if not value_str or not isinstance(value_str, str):
        return False
    cleaned = value_str.replace(",", "").replace(" ", "").replace(".", "")
    return cleaned.isdigit() and int(cleaned) >= 0


def is_hidden_element(element):
    style = element.get("style", "")
    normalized = style.replace(" ", "").replace("'", "").replace('"', '')
    return "display:none" in normalized


def parse():
    base_url = "https://supremevalues.com/mm2/"

    categories = [
        "godlies",
        "ancients",
        "vintages",
        "uniques",
        "chromas",
        "legendaries",
        "rares",
        "uncommons",
        "commons",
        "pets",
        "misc",
    ]

    all_items = []
    driver = get_driver()

    try:
        for cat in categories:
            url = f"{base_url}{cat}"

            driver.get(url)

            time.sleep(4)

            html = driver.page_source
            soup = BeautifulSoup(html, "html.parser")

            if "Incapsula" in soup.text or "incident_id" in html:
                return None

            items = soup.find_all("div", class_="itemcolumn")

            if not items:
                continue

            for item in items:
                if is_hidden_element(item):
                    continue

                head_div = item.find("div", class_="itemhead")
                if not head_div:
                    continue
                name = head_div.get_text(strip=True)

                item_type = cat

                val_tag = item.find(class_="itemvalue")
                value = val_tag.get_text(strip=True) if val_tag else "0"

                if not is_pure_numeric(value):
                    continue

                normalized_value = int(
                    value.replace(",", "").replace(" ", "").replace(".", "")
                )

                stab_tag = item.find(class_="itemstability")
                stability = stab_tag.get_text(strip=True) if stab_tag else "Stable"

                demand = "N/A"
                demand_text = item.find(string=lambda text: text and "Demand -" in text)
                if demand_text:
                    demand_tag = demand_text.find_next("b")
                    if demand_tag:
                        demand = demand_tag.get_text(strip=True)

                rarity = "N/A"
                rarity_text = item.find(string=lambda text: text and "Rarity -" in text)
                if rarity_text:
                    rarity_tag = rarity_text.find_next("b")
                    if rarity_tag:
                        rarity = rarity_tag.get_text(strip=True)

                item_data = {
                    "name": name,
                    "type": item_type,
                    "value": normalized_value,
                    "stability": stability,
                    "demand": demand,
                    "rarity": rarity,
                }
                all_items.append(item_data)

    except Exception as e:
        print(f"selenium error: {e}")

    finally:
        driver.quit()

    if all_items:
        output_path = os.path.join(os.path.dirname(__file__), "mm2_values.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(all_items, f, ensure_ascii=False, indent=2)
        print(f"\nsaved {len(all_items)} items to {output_path}")

    return all_items


if __name__ == "__main__":
    parse()