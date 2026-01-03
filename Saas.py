
import asyncio
from playwright.async_api import async_playwright
from fastapi import FastAPI
import uvicorn
import os

app = FastAPI()

@app.get("/")
async def root():
    return {"message": "API is online", "status": "running"}

async def scrape_leads(niche: str, location: str):
    async with async_playwright() as p:
        # Launch with no-sandbox (essential for Railway/Docker)
        browser = await p.chromium.launch(headless=True, args=["--no-sandbox"])
        
        # FIXED: Awaiting both context and page creation separately
        # Added a common User-Agent to look more like a real browser
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        )
        page = await context.new_page()
        
        search_url = f"https://www.yellowpages.com/search?search_terms={niche}&geo_location_terms={location}"
        
        leads = []
        try:
            # Set a longer timeout for slow directory sites
            await page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
            
            # Wait for the results to actually load on the screen
            await page.wait_for_selector(".result", timeout=15000)
            elements = await page.query_selector_all(".result")
            
            for el in elements[:15]: 
                name_el = await el.query_selector(".business-name")
                phone_el = await el.query_selector(".phones")
                
                if name_el:
                    name_text = await name_el.inner_text()
                    phone_text = await phone_el.inner_text() if phone_el else "N/A"
                    
                    leads.append({
                        "name": name_text.strip(),
                        "phone": phone_text.strip()
                    })
            
            return leads

        except Exception as e:
            print(f"Scrape Error: {e}")
            return [{"error": "Could not find leads. The site might be blocking the request or the niche/location is invalid."}]
        
        finally:
            # Ensure the browser always closes to save Railway memory
            await browser.close()

@app.get("/get-leads")
async def get_leads_api(niche: str, location: str):
    # This is the endpoint you connect to RapidAPI
    data = await scrape_leads(niche, location)
    return {
        "status": "success", 
        "count": len(data), 
        "data": data
    }

if __name__ == "__main__":
    # Use Railway's dynamic port or default to 8080
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port)
