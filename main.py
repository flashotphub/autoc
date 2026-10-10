import asyncio
import os
import random
import string
import uuid
from datetime import datetime, timezone
from pathlib import Path
from collections import deque
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field, AliasChoices
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="Signup Sequential QA", version="1.0.0")
REFERRAL_URL = os.getenv("REFERRAL_URL", "https://paid2watch.co/r/78jnd")
SIGNUP_URL = os.getenv("SIGNUP_URL", "https://paid2watch.co/#/signup")
FIXED_PASSWORD = "ik@raj12"
NAMES = ["Akash", "Raju", "Nisha", "Priya", "Rahul", "Ananya", "Rohan", "Maya", "Amit", "Neha", "Arjun", "Puja", "Sayan", "Riya", "Kunal", "Tina"]
events = deque(maxlen=300)

class BatchRequest(BaseModel):
    # Defaults and aliases make the endpoint tolerant of older dashboard versions.
    count: int = Field(default=1, ge=1, le=25, validation_alias=AliasChoices("count", "batch_count", "sequential_count"))
    referral_code: str = Field(default="", max_length=100, validation_alias=AliasChoices("referral_code", "referralCode", "ref_code"))
    referrer_id: str = Field(default="", max_length=100, validation_alias=AliasChoices("referrer_id", "referrerId", "ref_id"))

@app.get("/")
def home():
    return FileResponse(BASE_DIR / "index.html")

@app.get("/health")
def health():
    return {"ok": True, "browser": "Playwright Chromium headless", "mode": "sequential signup QA", "captcha_bypass": False}

@app.get("/api/events")
def get_events():
    return {"events": list(events)}

@app.post("/api/run-batch")
async def run_batch(request: Request):
    # Parse manually so bad/missing JSON returns a useful response instead of FastAPI's
    # generic 422 HTML/JSON error. Accept JSON and form submissions from older UIs.
    try:
        content_type = request.headers.get("content-type", "").lower()
        if "application/json" in content_type:
            payload = await request.json()
        elif "application/x-www-form-urlencoded" in content_type or "multipart/form-data" in content_type:
            payload = dict(await request.form())
        else:
            raw = await request.body()
            if not raw:
                payload = {}
            else:
                try:
                    import json
                    payload = json.loads(raw.decode("utf-8"))
                except Exception:
                    return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid request body. Send JSON with count, referral_code, and referrer_id."})
        if not isinstance(payload, dict):
            return JSONResponse(status_code=400, content={"ok": False, "error": "Request body must be a JSON object."})
        req = BatchRequest.model_validate(payload)
    except Exception as exc:
        # Return explicit validation details to help fix the form rather than a generic 422.
        detail = getattr(exc, "errors", None)
        errors = detail() if callable(detail) else None
        return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid batch settings", "details": errors or str(exc)[:300], "expected": {"count": "integer 1–25 (defaults to 1)", "referral_code": "string (optional)", "referrer_id": "string (optional)"}})

    run_id = uuid.uuid4().hex[:10]
    results = []
    browser = None
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            for i in range(req.count):
                email = f"qa.{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}.{i+1}.{''.join(random.choices(string.ascii_lowercase+string.digits,k=5))}@example.com"
                name = random.choice(NAMES) + " " + random.choice(["Das", "Roy", "Sen", "Dutta", "Mondal", "Paul"])
                context = await browser.new_context()
                page = await context.new_page()
                console_errors = []
                page.on("pageerror", lambda exc: console_errors.append(str(exc)[:200]))
                item = {"number": i + 1, "name": name, "email": email, "status": "started"}
                try:
                    await page.goto(REFERRAL_URL, wait_until="domcontentloaded", timeout=60000)
                    await page.wait_for_timeout(1000)
                    storage_before = await page.evaluate("() => Object.fromEntries(Object.entries(localStorage))")
                    # Populate only existing referral-related storage keys; don't invent unknown app-specific keys.
                    referral_keys = [k for k in storage_before if any(s in k.lower() for s in ("refer", "referrer", "invite", "affiliate"))]
                    if req.referral_code:
                        for key in referral_keys:
                            if "code" in key.lower() or "refer" in key.lower() or "invite" in key.lower():
                                await page.evaluate("([k,v]) => localStorage.setItem(k,v)", [key, req.referral_code])
                    if req.referrer_id:
                        for key in referral_keys:
                            if "id" in key.lower() or "referrer" in key.lower() or "affiliate" in key.lower():
                                await page.evaluate("([k,v]) => localStorage.setItem(k,v)", [key, req.referrer_id])
                    storage_after = await page.evaluate("() => Object.fromEntries(Object.entries(localStorage))")
                    await page.goto(SIGNUP_URL, wait_until="domcontentloaded", timeout=60000)
                    await page.locator("#display-name").fill(name, timeout=15000)
                    await page.locator("#email").fill(email, timeout=15000)
                    await page.locator("#password").fill(FIXED_PASSWORD, timeout=15000)
                    captcha_selector = 'iframe[src*="captcha" i], iframe[src*="recaptcha" i], iframe[src*="hcaptcha" i], iframe[src*="challenges.cloudflare.com" i], [data-sitekey], input[name*="captcha" i]'
                    captcha_detected = await page.locator(captcha_selector).count() > 0
                    item.update({"referral_storage_before": storage_before, "referral_storage_after": storage_after, "referral_keys_found": referral_keys, "captcha_detected": captcha_detected})
                    # Do not attempt to bypass CAPTCHA; if present, stop before submitting.
                    if captcha_detected:
                        item.update({"status": "captcha_detected", "message": "CAPTCHA detected; submission stopped. Solve it manually in an authorized test session."})
                        results.append(item)
                        events.appendleft({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), **{k:item.get(k) for k in ("number","name","email","status","message")}})
                        break
                    await page.get_by_role("button", name="Create free account").click(timeout=15000)
                    await page.wait_for_timeout(2500)
                    body = (await page.locator("body").inner_text())[:1600]
                    lower = body.lower()
                    captcha_after = await page.locator(captcha_selector).count() > 0 or any(s in lower for s in ("captcha", "verify you are human", "complete the challenge"))
                    success = any(s in lower for s in ("account created", "signup successful", "registration successful", "welcome"))
                    item.update({"status": "captcha_detected" if captcha_after else "success_text_detected" if success else "submitted_needs_review", "captcha_detected_after_submit": captcha_after, "page_text": body, "page_errors": console_errors[:5]})
                    results.append(item)
                    events.appendleft({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), **{k:item.get(k) for k in ("number","name","email","status")}})
                    if captcha_after:
                        break
                except PlaywrightTimeoutError as exc:
                    item.update({"status": "timeout", "message": str(exc)[:250]})
                    results.append(item)
                    events.appendleft({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), **{k:item.get(k) for k in ("number","name","email","status","message")}})
                    break
                except Exception as exc:
                    item.update({"status": "error", "message": f"{type(exc).__name__}: {str(exc)[:300]}"})
                    results.append(item)
                    events.appendleft({"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), **{k:item.get(k) for k in ("number","name","email","status","message")}})
                    break
                finally:
                    await context.close()
        return {"ok": bool(results) and all(x.get("status") == "success_text_detected" for x in results), "run_id": run_id, "requested": req.count, "completed": len(results), "results": results, "note": "Success-like page text is not proof of account creation; verify in your site database/admin. CAPTCHA is never bypassed."}
    except Exception as exc:
        return {"ok": False, "run_id": run_id, "error": f"{type(exc).__name__}: {str(exc)[:350]}", "completed": len(results), "results": results}
    finally:
        if browser:
            await browser.close()
